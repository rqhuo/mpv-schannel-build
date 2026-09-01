#!/usr/bin/env python3
"""
patch_build_ninja.py  —  v17 NINJA-DAG SURGERY

Replaces the COMMAND for every build rule whose output is a stamp file
belonging to one of our 29 whitelisted (non-critical) packages.

v15.2 fixes:
  - Handle MULTIPLE explicit outputs separated by SPACES:
      build out1 out2 : RULE deps
    (v15.1 only handled `out1 | out2` implicit outputs)
  - Whitelist match if ANY output matches (was only checking first)
  - Touch ALL outputs with `&&` chained commands

v16 fixes:
  - UNIVERSAL_SKIP_STEPS: patch removebuild + postremovebuild for ALL
    packages (not just whitelisted). These steps delete build dirs and
    lose compiled DLLs before copy-binary can run.

v16.4 fixes:
  - Insert rescue_cmd BEFORE closing quote of cmd.exe /C "..." instead
    of after it. cmd.exe /C quote rules merge appended content into the
    previous argument, causing `cmake -E touch "path && bash path"` to
    fail. Now: cmd.exe /C "...command && bash rescue_cp.sh"
  - Remove quotes around rescue_cp.sh path (inserted inside cmd.exe
    quotes, nested quotes would break parsing). Path has no spaces.

v17 fixes (CRITICAL — root cause of v16.x empty mpv-build dir):
  - NEVER_TOUCH_PKGS: mpv / mpv-release are EXCLUDED from EVERY patching
    path (whitelist PKGS, UNIVERSAL_SKIP_STEPS, SKIP_VIA_TOUCH_STEPS)
    EXCEPT for a tiny safe subset of steps that never affect DLL output
    (copy-binary / copy-package-dir / fullclean / liteclean / delete-dir).
    v16 accidentally short-circuited mpv-install to cmake echo_append
    and let configure/build *-.cmake scripts silently return 0, so meson
    setup + ninja never ran.  v17 guarantees mpv real work steps stay
    untouched and their COMMANDs execute verbatim.
  - mpv-rescue now fires TWICE: after -build (fresh compile) AND after
    -strip-binary (stripped/final size).  This maximises chance of
    preserving a libmpv-2.dll even if install/removebuild delete the
    build tree immediately afterwards.
  - ALWAYS_REMOVE_BUILD_DIRS interaction: superbuild compiles the
    ExternalProject_Add `removebuild` step into a side-effect that
    empties the build dir BEFORE re-configuring — we must NOT replace
    mpv/mpv-release removebuild/postremovebuild with cmake -E touch;
    instead, for these two packages ONLY we KEEP THE ORIGINAL COMMAND
    so downstream DAG logic is consistent.  (The rescue runs preserve
    the DLL regardless of later deletions.)

Usage:  python patch_build_ninja.py <build_dir>
"""
import re
import sys
import os

BUILD_DIR = sys.argv[1]
NINJA_FILE = os.path.join(BUILD_DIR, "build.ninja")

WHITELIST_PKGS = [
    # Tier 0: redundant mingw toolchain (8)
    "gcc-binutils", "gcc", "mingw-w64", "gcc-wrapper",
    "mingw-w64-headers", "mingw-w64-crt", "gendef", "cppwinrt",
    # Tier 1: confirmed FAILED (4)
    "shaderc", "opus", "bzip2", "libmodplug",
    # Tier 2: prophylactic (3)
    "xvidcore", "x265-10bit-lib", "x265-12bit-lib",
    # Tier 3: Vulkan/shader stack (8)
    "shaderc-utils", "spirv-cross", "glslang", "spirv-tools",
    "spirv-headers", "spirv-header", "vulkan-headers", "vulkan-header",
    # Tier 4: secondary audio/utility (4)
    "libopenmpt", "vapoursynth", "libplacebo",
    # Tier 5: crypto neighbours (2)
    "openssl", "mbedtls",
]

# ================================================================
# V17  NEVER, EVER touch these packages — they build our end-goal
#      libmpv-2.dll.  Their configure / build / install / strip /
#      force-meson-configure / check-git / write-head / patch /
#      download / update / mkdir / done  steps must run the ORIGINAL
#      COMMAND or the DAG lies about success.
#
#      The ONLY exceptions are housekeeping steps we explicitly
#      whitelist in NEVER_TOUCH_SAFE_SKIP (they never write object
#      code and often fail for cosmetic reasons on CI).
# ================================================================
NEVER_TOUCH_PKGS = ["mpv", "mpv-release"]

# Steps inside NEVER_TOUCH_PKGS that are STILL safe to cmake -E touch:
#   - copy-binary       → mpv-package/ dir missing in CI no-op layout
#   - copy-package-dir  → impl.cmake shell cmds fail in env w/o unzip/7z
#   - fullclean / liteclean / delete-dir / removeprefix
#                       → rm -rf helpers, no impact on build output
NEVER_TOUCH_SAFE_SKIP = [
    "copy-binary", "copy-package-dir",
    "fullclean", "liteclean", "delete-dir",
    "removeprefix", "removebuild", "postremovebuild",
]

# V16: UNIVERSAL skip steps — these steps are patched for ALL packages
# EXCEPT NEVER_TOUCH_PKGS (see NEVER_TOUCH_SAFE_SKIP above for the tiny
# subset of mpv/* steps we ARE still willing to short-circuit).
# - removebuild: deletes the package's build/ directory (loses .dll/.exe)
# - postremovebuild: post-cleanup after removebuild
UNIVERSAL_SKIP_STEPS = ["removebuild", "postremovebuild"]

# V16.1: Steps to skip via touch (non-whitelisted but cause failures)
# mpv-copy-binary fails because mpv-package/ dir doesn't exist.
# mpv-copy-package-dir fails because impl.cmake has shell commands.
# fullclean/liteclean/delete-dir may delete build dirs with our DLLs.
SKIP_VIA_TOUCH_STEPS = ["copy-binary", "copy-package-dir", "fullclean", "liteclean", "delete-dir"]

# V17: Removeprefix / strip build prefix — often destructive and
# unnecessary in CI.  Apply to all non-NEVER_TOUCH packages; for
# NEVER_TOUCH packages we also skip them (they're in SAFE_SKIP).
EXTRA_UNIVERSAL_SKIP = ["removeprefix"]

# V16.1 + v17: After these build steps complete, immediately copy DLL
# to rescue dir.  v17 runs rescue TWICE per package because:
#   1) right after `-build`  →  unstripped DLL is present
#   2) right after `-strip-binary` → stripped DLL is present
# If install runs `meson install` + ALWAYS_REMOVE_BUILD_DIRS deletes
# the build dir between steps 1 and later diagnostics, at least one
# rescue copy will have won the race.
RESCUE_AFTER_BUILD = {
    "mpv-build": {
        "dll": "packages/mpv-prefix/src/mpv-build/libmpv-2.dll",
        "exe": "packages/mpv-prefix/src/mpv-build/mpv.exe",
        "impla": "packages/mpv-prefix/src/mpv-build/libmpv.dll.a",
        "tag": "post-build",
    },
    "mpv-release-build": {
        "dll": "packages/mpv-release-prefix/src/mpv-release-build/libmpv-2.dll",
        "exe": "packages/mpv-release-prefix/src/mpv-release-build/mpv.exe",
        "impla": "packages/mpv-release-prefix/src/mpv-release-build/libmpv.dll.a",
        "tag": "post-build",
    },
    "mpv-strip-binary": {
        "dll": "packages/mpv-prefix/src/mpv-build/libmpv-2.dll",
        "exe": "packages/mpv-prefix/src/mpv-build/mpv.exe",
        "impla": "packages/mpv-prefix/src/mpv-build/libmpv.dll.a",
        "tag": "post-strip",
    },
    "mpv-release-strip-binary": {
        "dll": "packages/mpv-release-prefix/src/mpv-release-build/libmpv-2.dll",
        "exe": "packages/mpv-release-prefix/src/mpv-release-build/mpv.exe",
        "impla": "packages/mpv-release-prefix/src/mpv-release-build/libmpv.dll.a",
        "tag": "post-strip",
    },
}

# Build a regex that matches: <pkg>-stamp[/\<]pkg>-<step>
def make_pkg_regex(pkg):
    esc = re.escape(pkg)
    return re.compile(rf'{esc}-stamp[/\\]{esc}-')

PKG_REGEXES = [(pkg, make_pkg_regex(pkg)) for pkg in WHITELIST_PKGS]

def parse_outputs(output_raw):
    """Parse all outputs from a ninja build statement output field.

    Ninja syntax:
      build out1 out2 | out3 out4 || out5 : RULE deps

    - out1 out2: explicit outputs (space-separated)
    - out3 out4: implicit outputs (after |, space-separated)
    - out5: order-only output (after ||)

    Returns list of all output paths that need to be touched.
    """
    all_outputs = []
    # Split on || first (order-only), take left part
    part = output_raw.split('||')[0]
    # Split on | (implicit outputs)
    for sub in part.split('|'):
        for tok in sub.split():
            tok = tok.strip()
            if tok:
                all_outputs.append(tok)
    return all_outputs

def is_never_touch_output(out_norm):
    """Check if a single output belongs to a NEVER_TOUCH_PKG and is NOT in
    the NEVER_TOUCH_SAFE_SKIP whitelist. Returns:
      - 'core_step'  → NEVER_TOUCH pkg + NOT safe-skip → DO NOT TOUCH
      - 'safe_skip'  → NEVER_TOUCH pkg + safe-skip     → OK to touch
      - None         → not a NEVER_TOUCH pkg            → normal check
    """
    for nt_pkg in NEVER_TOUCH_PKGS:
        # Match: <...>/<nt_pkg>-stamp/<nt_pkg>-<step>
        stamp_pattern = f'/{nt_pkg}-stamp/{nt_pkg}-'
        if stamp_pattern in out_norm:
            # Extract step suffix after /<nt_pkg>-stamp/<nt_pkg>-
            idx = out_norm.index(stamp_pattern) + len(stamp_pattern)
            step = out_norm[idx:]
            # Also strip any trailing slash content (unlikely for stamps)
            step = step.split('/')[0].split('\\')[0]
            if step in NEVER_TOUCH_SAFE_SKIP:
                return 'safe_skip'
            else:
                # configure, build, install, strip-binary, patch, download,
                # update, mkdir, write-head, force-meson-configure, check-git,
                # copy-versionfile, done, etc. → MUST NOT be touched.
                return 'core_step'
    return None

def is_whitelisted_output(outputs):
    """Return True if ANY output should be short-circuited to cmake -E touch.

    Priority (V17 critical fix):
      1. NEVER_TOUCH_PKGS (mpv, mpv-release) CORE steps → False (NO touch)
      2. NEVER_TOUCH_PKGS SAFE_SKIP steps              → True  (OK touch)
      3. WHITELIST_PKGS any step                        → True  (touch)
      4. UNIVERSAL_SKIP_STEPS (removebuild, postremovebuild) → True
      5. SKIP_VIA_TOUCH_STEPS (copy-binary, etc.)       → True
      6. EXTRA_UNIVERSAL_SKIP (removeprefix)            → True
    """
    has_never_touch_safe = False
    for out in outputs:
        out_norm = out.replace('\\', '/')

        # ---- V17: NEVER_TOUCH_PKGS take absolute priority ----
        nt_status = is_never_touch_output(out_norm)
        if nt_status == 'core_step':
            # mpv/mpv-release real-work step: REFUSE touch, no further check
            return False
        if nt_status == 'safe_skip':
            has_never_touch_safe = True
            # continue checking; safe-skip will be combined with universal
            # skip checks below to return True

        # V16: check whitelist packages
        for pkg, regex in PKG_REGEXES:
            if regex.search(out_norm):
                return True
        # V16: check universal skip steps (removebuild, postremovebuild)
        for step in UNIVERSAL_SKIP_STEPS:
            if out_norm.endswith('-' + step):
                return True
        # V16.1: check skip-via-touch steps (copy-binary etc.)
        for step in SKIP_VIA_TOUCH_STEPS:
            if out_norm.endswith('-' + step):
                return True
        # V17: EXTRA_UNIVERSAL_SKIP (removeprefix) — previously unused
        for step in EXTRA_UNIVERSAL_SKIP:
            if out_norm.endswith('-' + step):
                return True

    # If any output was a NEVER_TOUCH safe-skip (and no core_step vetoed),
    # allow touching it.
    if has_never_touch_safe:
        return True
    return False

def is_rescue_step(outputs):
    """Return the rescue step name if ANY output matches RESCUE_AFTER_BUILD,
    else None."""
    for out in outputs:
        out_norm = out.replace('\\', '/')
        basename = out_norm.split('/')[-1]
        for step_name in RESCUE_AFTER_BUILD:
            if basename == step_name:
                return step_name
    return None

def make_rescue_command(step_name, build_dir):
    """Return a bash command that runs rescue_cp.sh script.
    The script is written to disk by write_rescue_script().
    V16.4: No quotes around path (inserted inside cmd.exe /C "...")
    so nested quotes would break cmd.exe parsing. Path has no spaces."""
    rescue_script = os.path.join(build_dir, 'rescue_cp.sh').replace('\\', '/')
    return f'bash {rescue_script}'

def write_rescue_script(build_dir):
    """Write rescue_cp.sh to the build directory.
    This script scans known locations for libmpv-2.dll and copies it
    (along with mpv.exe, libmpv.dll.a, client.h) to rescue/ subdir."""
    script_content = '''#!/bin/bash
# rescue_cp.sh - V16.3 rescue script
# Called from ninja COMMAND after mpv-build/mpv-release-build succeeds.
# Scans known locations for libmpv-2.dll and copies to rescue/ dir.
set +e
SB_BUILD="$(cd "$(dirname "$0")" && pwd)"
cd "$SB_BUILD" || exit 0
mkdir -p rescue
RESCUED=0
for src in \\
  "packages/mpv-prefix/src/mpv-build/libmpv-2.dll" \\
  "packages/mpv-release-prefix/src/mpv-release-build/libmpv-2.dll" \\
  "local32/bin/libmpv-2.dll" \\
  "install/bin/libmpv-2.dll"; do
  if [ -f "$src" ]; then
    cp -f "$src" rescue/libmpv-2.dll
    echo "RESCUE: copied $src -> rescue/libmpv-2.dll"
    srcdir="$(dirname "$src")"
    [ -f "$srcdir/mpv.exe" ] && cp -f "$srcdir/mpv.exe" rescue/ 2>/dev/null
    [ -f "$srcdir/libmpv.dll.a" ] && cp -f "$srcdir/libmpv.dll.a" rescue/ 2>/dev/null
    [ -f "$srcdir/client.h" ] && cp -f "$srcdir/client.h" rescue/ 2>/dev/null
    RESCUED=1
    break
  fi
done
if [ "$RESCUED" = "0" ]; then
  echo "RESCUE: libmpv-2.dll not found in any expected location"
fi
exit 0
'''
    script_path = os.path.join(build_dir, 'rescue_cp.sh')
    with open(script_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(script_content)
    print(f"v16.3 rescue_cp.sh written to {script_path}")
    return script_path

def make_touch_command(outputs):
    """Build a `cmake -E touch` chain for all outputs."""
    touches = []
    for out in outputs:
        out_path = out.replace('\\', '/')
        touches.append(f'cmake -E touch "{out_path}"')
    return ' && '.join(touches)

# Write rescue_cp.sh script BEFORE patching build.ninja
write_rescue_script(BUILD_DIR)

# Read build.ninja
with open(NINJA_FILE, 'r', encoding='utf-8', errors='replace') as f:
    lines = f.readlines()

new_lines = []
patched = 0
rerun_patched = False
i = 0
n = len(lines)

while i < n:
    line = lines[i]

    # ----------------------------------------------------------
    # 1) Disable RERUN_CMAKE rule
    # ----------------------------------------------------------
    if line.strip().startswith('rule RERUN_CMAKE'):
        new_lines.append(line)
        i += 1
        while i < n:
            bl = lines[i]
            if bl.strip().startswith('command =') or bl.strip().startswith('command='):
                new_lines.append('  command = cmake -E echo "RERUN_CMAKE disabled by v15 patch"\n')
                rerun_patched = True
                i += 1
                break
            elif bl.startswith('rule ') or bl.startswith('build '):
                break
            else:
                new_lines.append(bl)
                i += 1
        continue

    # Disable RERUN_CMAKE build statement
    if re.match(r'^build\s+.+?:\s*RERUN_CMAKE\b', line):
        new_lines.append(line)
        i += 1
        while i < n:
            bl = lines[i]
            if bl.strip().startswith('COMMAND') or bl.strip().startswith('command'):
                new_lines.append('  COMMAND = cmake -E echo "RERUN_CMAKE build disabled by v15"\n')
                rerun_patched = True
                i += 1
                break
            elif bl.startswith('build ') or bl.startswith('rule '):
                break
            else:
                new_lines.append(bl)
                i += 1
        continue

    # ----------------------------------------------------------
    # 2) Patch whitelisted package build rules
    # ----------------------------------------------------------
    build_match = re.match(r'^build\s+(.+?)\s*:\s+\w+', line)
    if build_match:
        output_raw = build_match.group(1).strip()
        # v15.2: parse ALL outputs (space + | separated)
        outputs = parse_outputs(output_raw)

        if not outputs:
            new_lines.append(line)
            i += 1
            continue

        # V16.1: Check if this is a rescue step (mpv-build / mpv-release-build)
        # If so, append cp command to the END of the existing COMMAND to
        # immediately copy DLL to rescue/ dir the instant it's compiled.
        rescue_step = is_rescue_step(outputs)
        if rescue_step:
            rescue_cmd = make_rescue_command(rescue_step, BUILD_DIR)
            new_lines.append(line)
            i += 1
            found_command = False
            while i < n:
                bl = lines[i]
                if bl.startswith('build ') or bl.startswith('rule ') or \
                   (bl and not bl[0] in (' ', '\t') and not bl.startswith('#')):
                    break

                if bl.strip().startswith('COMMAND') or bl.strip().startswith('command'):
                    # Read the full COMMAND (may span multiple lines with $)
                    cmd_lines = [bl]
                    i += 1
                    while i < n and cmd_lines[-1].rstrip().endswith('$'):
                        cmd_lines.append(lines[i])
                        i += 1
                    # Append rescue cp to the last line of COMMAND.
                    # V16.4 fix: Insert rescue_cmd BEFORE the closing quote (")
                    # of cmd.exe /C "..." to avoid cmd.exe quote parsing issues.
                    # If we append AFTER the closing ", cmd.exe /C's quote
                    # rules merge it into the previous argument, causing
                    # `cmake -E touch "path && bash path"` to fail.
                    last_line = cmd_lines[-1]
                    if last_line.rstrip().endswith('$'):
                        last_line = last_line.rstrip()[:-1]  # remove $
                    else:
                        last_line = last_line.rstrip('\n')
                    # Find last " and insert rescue_cmd before it
                    # (cmd.exe /C "...command..." -> "...command && bash rescue_cp.sh")
                    # If no closing quote found, just append normally
                    stripped = last_line.rstrip()
                    if stripped.endswith('"'):
                        # Insert before the closing quote
                        insert_pos = len(stripped) - 1
                        last_line = stripped[:insert_pos] + f' && {rescue_cmd}' + stripped[insert_pos:] + '\n'
                    else:
                        last_line = stripped + f' && {rescue_cmd}\n'
                    # Replace last line
                    cmd_lines[-1] = last_line
                    new_lines.extend(cmd_lines)
                    patched += 1
                    found_command = True
                    break
                else:
                    new_lines.append(bl)
                    i += 1

            if not found_command:
                new_lines.append(f'  COMMAND = {rescue_cmd}\n')
                patched += 1
            continue

        # v15.2: whitelist match if ANY output matches
        if is_whitelisted_output(outputs):
            new_lines.append(line)
            i += 1

            touch_cmd = make_touch_command(outputs)
            found_command = False
            while i < n:
                bl = lines[i]
                if bl.startswith('build ') or bl.startswith('rule ') or \
                   (bl and not bl[0] in (' ', '\t') and not bl.startswith('#')):
                    break

                if bl.strip().startswith('COMMAND') or bl.strip().startswith('command'):
                    new_lines.append(f'  COMMAND = {touch_cmd}\n')
                    patched += 1
                    found_command = True
                    i += 1
                    while i < n and lines[i-1].rstrip().endswith('$'):
                        i += 1
                    break
                else:
                    new_lines.append(bl)
                    i += 1

            if not found_command:
                new_lines.append(f'  COMMAND = {touch_cmd}\n')
                patched += 1
            continue

    new_lines.append(line)
    i += 1

# Write the patched build.ninja
with open(NINJA_FILE, 'w', encoding='utf-8', newline='\n') as f:
    f.writelines(new_lines)

print(f"v17 patch_build_ninja.py: patched {patched} build rule COMMANDs -> cmake -E touch chain")
print(f"v17 patch_build_ninja.py: NEVER_TOUCH_PKGS = {NEVER_TOUCH_PKGS} (core steps PROTECTED)")
print(f"v17 patch_build_ninja.py: RERUN_CMAKE disabled = {rerun_patched}")
print(f"v17 patch_build_ninja.py: file = {NINJA_FILE}")
