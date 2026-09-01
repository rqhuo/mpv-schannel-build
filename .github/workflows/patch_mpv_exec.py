#!/usr/bin/env python3
"""
patch_mpv_exec.py — V18 bypass for broken build/exec PE32 wrapper.

ROOT CAUSE (logs_90858810948):
  build/exec (exec_wrapper.c PE32) spawns bash via _spawnvp, but bash
  either can't be found on PATH in the cmake execute_process context,
  or the Windows-style backslash path to exec.sh breaks MSYS2 bash's
  source command. Result: exec_wrapper exits 0 in 0.0003s, meson/ninja
  never run, configure-out.log / build-out.log are 0 bytes.

FIX:
  1. Write mpv_run.sh to the build directory — a bash script that does
     exactly what exec_wrapper.kScript did, but as a native bash script:
       _CMD=("$@"); set --; . exec.sh; eval "$(printf ' %q' "${_CMD[@]}")"
  2. Patch every mpv/mpv-release *-configure-.cmake and *-build-.cmake
     file: replace "D:/.../build/exec" with "bash" and insert the path
     to mpv_run.sh as the next semicolon-separated argument.

  After patching, cmake's execute_process will call:
     bash  D:/.../mpv_run.sh  CONF=1  meson  setup  ...
  instead of:
     D:/.../build/exec  CONF=1  meson  setup  ...

  bash is guaranteed to be on PATH (we verified: which meson => /mingw32/bin/meson).
"""
import os
import re
import sys
import glob

BUILD_DIR = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('SB_BUILD', '.')
BUILD_DIR = os.path.abspath(BUILD_DIR).replace('\\', '/')

MPV_RUN_SH = os.path.join(BUILD_DIR, 'mpv_run.sh').replace('\\', '/')

# --- 1. Write mpv_run.sh ---
mpv_run_content = '''#!/bin/bash
# mpv_run.sh - V18 replacement for build/exec PE32 wrapper
# Usage: bash mpv_run.sh <command...>
# Sources exec.sh for environment, then evals the command safely.
set +e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Save user args before sourcing exec.sh (exec.sh ends with `eval $*`)
_CMD=("$@")
set --
# Source the original exec.sh (sets PATH, PKG_CONFIG, etc.)
source "$SCRIPT_DIR/exec.sh"
# Safely re-quote and eval the user command
eval "$(printf ' %q' "${_CMD[@]}")"
exit $?
'''

with open(MPV_RUN_SH, 'w', encoding='utf-8', newline='\n') as f:
    f.write(mpv_run_content)
os.chmod(MPV_RUN_SH, 0o755)
print(f"v18 mpv_run.sh written to {MPV_RUN_SH}")

# --- 2. Patch configure-.cmake and build-.cmake files ---
# Find all mpv/mpv-release stamp directories
stamp_dirs = []
for pattern in [
    f'{BUILD_DIR}/packages/mpv-prefix/src/mpv-stamp',
    f'{BUILD_DIR}/packages/mpv-release-prefix/src/mpv-release-stamp',
]:
    if os.path.isdir(pattern):
        stamp_dirs.append(pattern)
        print(f"v18 found stamp dir: {pattern}")
    else:
        print(f"v18 WARNING: stamp dir not found: {pattern}")

if not stamp_dirs:
    print("v18 ERROR: No mpv/mpv-release stamp directories found!")
    sys.exit(1)

# Pattern to match build/exec path in set(command "...") lines
# The path looks like: D:/a/.../mpv-winbuild/build/exec
# It's always the first element before the first semicolon
exec_path_re = re.compile(
    r'(set\s*\(\s*command\s+")[^"]*?/build/exec([^"]*")',
    re.IGNORECASE
)

patched_files = 0
patched_lines = 0

for stamp_dir in stamp_dirs:
    # Patch all *-configure-.cmake and *-build-.cmake files
    for cmake_file in sorted(glob.glob(f'{stamp_dir}/*configure-.cmake') +
                             glob.glob(f'{stamp_dir}/*build-.cmake')):
        if not os.path.isfile(cmake_file):
            continue

        with open(cmake_file, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        original_content = content

        # Find and replace the set(command "...build/exec...") line
        def replace_exec(match):
            global patched_lines
            after_exec = match.group(2)  # e.g., ;CONF=1;meson;setup;..."
            patched_lines += 1
            return f'set(command "bash;{MPV_RUN_SH}{after_exec}'

        content = exec_path_re.sub(replace_exec, content)

        # V18.1: Remove meson options invalid for mpv v0.41.0
        # - -Dopenssl=disabled : removed in v0.37+
        # - -Dsubrandr=enabled : not available in v0.41.0
        # - -Dtls-backend=...  : unknown in v0.41.0 meson options for plain mpv
        invalid_opts = [
            ';-Dopenssl=disabled',
            ';-Dsubrandr=enabled',
            ';-Dsubrandr=disabled',
            ';-Dtls-backend=schannel',
            ';-Dtls-backend=openssl',
            ';-Dtls-backend=gnutls',
            ';-Dtls-backend=auto',
            ';-Dtls-backend=disabled',
        ]
        for opt in invalid_opts:
            if opt in content:
                content = content.replace(opt, '')
                print(f"v18.1 REMOVED invalid option '{opt}' from {os.path.basename(cmake_file)}")

        # V18.3: Fix merged gl+egl-angle option.
        # On CI (logs_90887073920), meson sees:  '-Dgl=enabled -Degl-angle=enabled'
        # i.e. a SINGLE shell-quoted argument with embedded space, causing
        # meson to parse gl="enabled -Degl-angle=enabled" (invalid choice).
        #
        # Two-step fix:
        #   FORM-A : cmake list element is explicitly shell-quoted:
        #            ;'-Dgl=<x> -Degl-angle=<y>'   (or ;"OPT1 OPT2")
        #            Fix: remove quotes, split with semicolon:
        #            ;-Dgl=<x>;-Degl-angle=<y>
        #   FORM-B : residual space-joined WITHOUT any quotes:
        #            -Dgl=<x> -Degl-angle=<y>
        #            Fix: insert semicolon between:
        #            -Dgl=<x>;-Degl-angle=<y>
        # Already-correct (;-Dgl=<x>;-Degl-angle=<y>) form is never rewritten.

        # --- Step 1: FORM-A (shell-quoted merged element)
        merged_gl_quote = re.compile(
            r";(['\"])(-Dgl=(enabled|disabled|auto)) (-Degl-angle=(enabled|disabled|auto))\1"
        )
        matches_a = merged_gl_quote.findall(content)
        if matches_a:
            def fix_form_a(m):
                return f';{m.group(2)};{m.group(4)}'
            content = merged_gl_quote.sub(fix_form_a, content)
            print(f"v18.3 FIXED merged gl+egl-angle FORM-A (quoted element) in {os.path.basename(cmake_file)}: {len(matches_a)} occurrence(s)")

        # --- Step 2: FORM-B (space-joined, no quotes)
        merged_plain = re.compile(
            r"(-Dgl=(enabled|disabled|auto)) (-Degl-angle=(enabled|disabled|auto))"
        )
        matches_b = merged_plain.findall(content)
        if matches_b:
            def fix_form_b(m):
                return f'{m.group(1)};{m.group(3)}'
            new_content = merged_plain.sub(fix_form_b, content)
            if new_content != content:
                content = new_content
                print(f"v18.3 FIXED merged gl+egl-angle FORM-B (space-joined) in {os.path.basename(cmake_file)}: {len(matches_b)} occurrence(s)")

        # --- Diagnostics: show current gl/egl-angle tokens for visual confirmation
        gl_diag_re = re.compile(r"[^;]*(?:gl|egl-angle)[^;\"]*", re.IGNORECASE)
        diags = list(set(gl_diag_re.findall(content)))
        snippets = []
        for d in diags:
            d = d.strip("\"' ")
            if d and ('gl' in d.lower() or 'egl' in d.lower()):
                snippets.append(d)
        if snippets:
            short = ' | '.join(snippets)
            if len(short) > 260:
                short = short[:260] + '...'
            print(f"v18.3 DIAG: gl/egl-angle tokens in {os.path.basename(cmake_file)}: {short}")

        if content != original_content:
            with open(cmake_file, 'w', encoding='utf-8', newline='\n') as f:
                f.write(content)
            patched_files += 1
            print(f"v18 PATCHED: {os.path.basename(cmake_file)}")
            # Show a snippet of the patched line for verification
            for line in content.split('\n'):
                if 'set(command' in line.lower() and 'bash' in line:
                    snippet = line[:200] + ('...' if len(line) > 200 else '')
                    print(f"  -> {snippet}")
                    break
        else:
            # Check if the file already has bash (already patched)
            has_bash = False
            for line in content.split('\n'):
                if 'set(command' in line.lower() and 'bash' in line:
                    has_bash = True
                    break
            if has_bash:
                print(f"v18 SKIP (already patched): {os.path.basename(cmake_file)}")
            else:
                print(f"v18 WARNING: no build/exec found in {os.path.basename(cmake_file)}")

print(f"v18 patch_mpv_exec.py: patched {patched_files} files, {patched_lines} set(command) lines")
print(f"v18 mpv_run.sh = {MPV_RUN_SH}")
