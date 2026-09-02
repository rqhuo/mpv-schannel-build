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

        # V18.6: Disable pdf-build. llvm-libcxx (libc++) is frequently the
        # first / earliest FAILED external step on the MSYS2 MINGW32 i686
        # toolchain because its install step expects libc++ headers and
        # runtime that have not been shipped / linked the way CMake's
        # superbuild layout assumes. Even if we short-circuit llvm-libcxx
        # stamps via patch_build_ninja.py whitelist so ninja doesn't abort
        # the whole DAG, meson's pdf-build feature still probes for C++
        # headers and may fail downstream. PDF rendering is a niche
        # subtitle feature and completely unnecessary for Blu-ray / DVD
        # playback (the user's end goal). Disable it to sever this
        # dependency chain entirely.
        #
        # Python `re` doesn't support variable-width look-behind, so we
        # do a plain regex match on `-Dpdf-build=<value>` with explicit
        # separator chars allowed BEFORE and AFTER as capture groups, and
        # re-emit the original separators unchanged.
        pdf_build_re = re.compile(
            r"([;\"'\s]|^)-Dpdf-build=(enabled|disabled|auto|true|false)([;\"'\s]|$)",
        )
        def _disable_pdf(m):
            before, _value, after = m.group(1), m.group(2), m.group(3)
            return f"{before}-Dpdf-build=disabled{after}"
        content_new, fixed_count = pdf_build_re.subn(_disable_pdf, content)
        if fixed_count:
            content = content_new
            print(f"v18.6 FIXED pdf-build: set {fixed_count} occurrence(s) to disabled in {os.path.basename(cmake_file)} (llvm-libcxx dep removed)")

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

# ---------------------------------------------------------------------------
# V18.4: Fix meson_cross.txt — replace /bin/i686-gcc /bin/i686-g++ /bin/i686-ar
# /bin/i686-strip etc with actual MSYS2 triplet: i686-w64-mingw32-<tool>.
#
# ROOT CAUSE (logs_90905341375):
#   meson cross file writes `/bin/i686-gcc` as c/cpp compiler, but MSYS2
#   doesn't provide that short name — the actual binary is
#   /mingw32/bin/i686-w64-mingw32-gcc. Result: meson says
#     "Detecting compiler via: `/bin/i686-gcc --version` -> Failed running
#      '/bin/i686-gcc', binary or interpreter not executable."
#   and meson setup aborts.
#
# We fix by rewriting any cross-file reference to the old short prefix
# `/bin/i686-<tool>` to the full triplet `i686-w64-mingw32-<tool>`.
# (Bash on MSYS2 can find unprefixed i686-w64-mingw32-gcc on PATH, so
#  absolute /bin/ is not required; meson resolves it correctly.)
# ---------------------------------------------------------------------------

cross_candidates = [
    os.path.join(BUILD_DIR, 'meson_cross.txt'),
    # Also try the sibling dir sometimes used
    os.path.join(os.path.dirname(BUILD_DIR), 'build', 'meson_cross.txt')
    if os.path.dirname(BUILD_DIR) else None,
]

cross_candidates = [p for p in cross_candidates if p and os.path.isfile(p)]

# If the fixed ones aren't found, search BUILD_DIR shallowly (1 level) for any
# meson_cross*.txt file:
if not cross_candidates:
    try:
        for name in os.listdir(BUILD_DIR):
            full = os.path.join(BUILD_DIR, name)
            if os.path.isfile(full) and 'meson_cross' in name and name.endswith('.txt'):
                cross_candidates.append(full)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# V18.5 CRITICAL: normalize paths and DE-DUPLICATE.
# The cross_candidates list above may produce IDENTICAL paths via multiple
# branches (e.g. BUILD_DIR == "foo/bar/build" and the sibling fallback also
# resolves to "foo/bar/build"). Processing the SAME meson_cross.txt twice
# is a DISASTER: V18.4 regex `/bin/i686-(TOOL)` matches INSIDE absolute
# paths written by V18.5 on pass #1 (e.g. .../mingw32/bin/i686-w64-mingw32-gcc.EXE)
# causing nonsense re-rewrite like:
#   c = 'D:/.../mingw32/bin/i686-w64-mingw32-gcc.EXE'
#   → pass#2 sees "i686-w64-mingw32-gcc.EXE" and replaces with "i686-w64-mingw32-w64-mingw32-gcc.EXE"
#   → c = 'D:/.../mingw32i686-w64-mingw32-w64-mingw32-gcc.EXE'  ← WRONG PATH
# Result: WinError 2 for every binary on meson detection.
# ---------------------------------------------------------------------------
def _norm(p):
    try:
        return os.path.normcase(os.path.abspath(p))
    except Exception:
        return p
_seen = set()
_uniq = []
for p in cross_candidates:
    key = _norm(p)
    if key not in _seen:
        _seen.add(key)
        _uniq.append(p)
if len(_uniq) != len(cross_candidates):
    print(f"v18.5 DEDUP: meson_cross candidates reduced {len(cross_candidates)} -> {len(_uniq)} (duplicates removed)")
cross_candidates = _uniq

if cross_candidates:
    import re as _re
    import shutil as _shutil
    # /bin/i686-gcc  /bin/i686-g++  /bin/i686-ar  /bin/i686-ld  /bin/i686-strip
    # /bin/i686-objcopy  /bin/i686-nm  /bin/i686-pkg-config etc.
    tool_re = _re.compile(r"/bin/i686-([A-Za-z0-9_.+\-]+)")

    # ---- V18.5 helper: resolve a tool name to existing Windows-native absolute ----
    # path. Python subprocess.Popen() (meson's backend) uses Windows'
    # CreateProcess, which only searches the real PATH and CWD (NOT bash
    # aliases, NOT /mingw32's virtual /bin redirects like Unix PATH semantics).
    # So when the user runs `which gcc` in bash and sees /mingw32/bin/gcc,
    # but Python on Windows looks for the same triplet binary and finds
    # nothing, we need to hand meson a REAL .exe path.
    def _resolve_tool(triplet_name, short_name):
        """
        Try, in order:
          A) shutil.which(triplet_name) — works when /mingw32/bin is on PATH
             (meson spawns via Windows native Python so PATH entries must be
              Windows paths — which MSYS2 setup usually ensures).
          B) shutil.which(short_name) — e.g. "ar" or "strip" living as
             /mingw32/bin/ar.exe without triplet prefix.
          C) Standard MSYS2 MINGW32 dirs for fallback:
             C1) <MSYSROOT>/mingw32/i686-w64-mingw32/bin/<triplet_name>.exe
                 (some binutils live in the target-triplet subdir, not in
                  the main mingw32/bin)
             C2) <MSYSROOT>/mingw32/bin/<short_name>.exe
        Returns absolute POSIX-style path (forward slashes) if found, else None.
        """
        # A + B
        for cand in (triplet_name, short_name):
            try:
                p = _shutil.which(cand)
                if p:
                    return p.replace('\\', '/')
            except Exception:
                pass
        # C: walk well-known roots
        # Derive MSYSROOT from known mingw python on the PATH or from env.
        roots = []
        try:
            py = _shutil.which("python") or _shutil.which("python3")
            if py:
                py = py.replace('\\', '/')
                # Typical: D:/a/_temp/msys64/mingw32/bin/python.exe -> ROOT=D:/a/_temp/msys64
                idx = py.rfind('/mingw32/bin/python')
                if idx > 0:
                    roots.append(py[:idx])  # e.g. D:/a/_temp/msys64
        except Exception:
            pass
        import os as _os
        for envk in ('MSYSTEM_PREFIX', 'MINGW_PREFIX', 'MSYS2_PATH'):
            v = _os.environ.get(envk)
            if v:
                v = v.replace('\\', '/').rstrip('/')
                if v.endswith('/mingw32'):
                    roots.append(v[:-len('/mingw32')])
                elif '/mingw' in v:
                    pass
        # Uniq while preserving order
        seen = set(); uniq_roots = []
        for r in roots:
            if r and r not in seen:
                seen.add(r); uniq_roots.append(r)
        probes = []
        for root in uniq_roots:
            probes.append(f"{root}/mingw32/i686-w64-mingw32/bin/{triplet_name}.exe")
            probes.append(f"{root}/mingw32/i686-w64-mingw32/bin/{short_name}.exe")
            probes.append(f"{root}/mingw32/bin/{short_name}.exe")
            probes.append(f"{root}/mingw32/bin/{triplet_name}.exe")
        for p in probes:
            try:
                if _os.path.isfile(p):
                    return p
            except OSError:
                pass
        return None

    cross_fixed_count = 0
    for cf in cross_candidates:
        try:
            with open(cf, 'r', encoding='utf-8') as f:
                data = f.read()
        except OSError as e:
            print(f"v18.4 cross-file: read error for {cf}: {e}")
            continue
        matches = tool_re.findall(data)
        if matches:
            def _replace(m):
                tool = m.group(1)
                # ------------------------------------------------------------------
                # V18.5 BULLETPROOF: only rewrite `.../bin/i686-<tool>` when it is
                # a standalone compiler entry (not a fully-qualified Windows path
                # like D:/.../mingw32/bin/i686-... — where /bin/i686- might appear
                # as a substring mid-path). Without this guard, re-applying V18.4
                # on an already-rewritten file (dedup failure, race, or manual
                # re-run) would incorrectly turn:
                #   c = 'D:/.../mingw32/bin/i686-w64-mingw32-gcc.EXE'
                # into:
                #   c = 'D:/.../mingw32i686-w64-mingw32-w64-mingw32-gcc.EXE'
                # (the '/bin/' part gets consumed because i686-w64-mingw32-gcc
                #  itself starts with "i686-..." giving a double prefix).
                # ------------------------------------------------------------------
                full = m.group(0)          # e.g. "/bin/i686-gcc"
                # Look at character immediately BEFORE the match boundary:
                start = m.start()
                if start > 0:
                    prev = data[start - 1]
                    # If the previous char is an ASCII letter/digit/"."/":" it
                    # means this occurrence is part of a longer absolute path
                    # segment (like "...mingw32/bin/i686-...") — skip it.
                    if prev.isalpha() or prev.isdigit() or prev == '.' or prev == ':':
                        return full
                # Also skip if there are more path components after the /bin
                # (meson_cross never writes "/bin/i686-foo/bar"). This check
                # catches most accidental mid-path overlaps directly.
                end = m.end()
                if end < len(data) and data[end] == '/':
                    return full
                return f"i686-w64-mingw32-{tool}"
            data = tool_re.sub(_replace, data)
            unique_tools = list(set(matches))
            print(f"v18.4 FIXED meson_cross: {cf}")
            print(f"v18.4   rewritten /bin/i686-* -> i686-w64-mingw32-* for tools: {', '.join(sorted(unique_tools))}")
        # ---- V18.5: verify each i686-w64-mingw32-<tool> really exists on ----
        # the native-Windows side (visible to Python subprocess / CreateProcess).
        # If not, resolve via _resolve_tool and substitute absolute path.
        #
        # Matches patterns in meson [binaries] sections like:
        #   c = 'i686-w64-mingw32-gcc'
        #   ar = "i686-w64-mingw32-ar"
        binaries_entry_re = _re.compile(
            r"""^(?P<lhs>[A-Za-z_][A-Za-z0-9_\-]*)\s*=\s*(['"])(?P<exe>i686-w64-mingw32-(?P<tool>[A-Za-z0-9_.+\-]+))\2"""
        )
        def _bin_fix(m):
            lhs = m.group('lhs')
            quote = m.group(2)
            old_name = m.group('exe')  # i686-w64-mingw32-ar
            tool = m.group('tool')      # ar
            abspath = _resolve_tool(old_name, tool)
            if abspath is None:
                # No replacement possible (not found); warn only
                print(f"v18.5 WARN: cannot resolve {old_name} -> leaving as-is")
                return m.group(0)
            print(f"v18.5 ABSOLUTE: {lhs:>8s} = {old_name:32s}  ->  {abspath}")
            return f"{lhs} = {quote}{abspath}{quote}"
        new_data_lines = []
        for line in data.splitlines():
            new_data_lines.append(binaries_entry_re.sub(_bin_fix, line))
        new_data = "\n".join(new_data_lines) + "\n"
        with open(cf, 'w', encoding='utf-8', newline='\n') as f:
            f.write(new_data)
        cross_fixed_count += 1
        # Visual sanity-check of compiler lines after applying all rewrites
        print(f"v18.4+V18.5 wrote meson_cross: {cf}")
        printed = 0
        for line in new_data.splitlines():
            low = line.lower()
            if any(k in low for k in ('gcc', 'g++', 'clang', 'ar =', 'ld =', 'strip',
                                       'objcopy', 'windres', 'pkgconf', 'pkg-config',
                                       'dlltool', 'nm =', 'ranlib', 'objdump')):
                print(f"  + {line.strip()[:200]}")
                printed += 1
                if printed >= 8:
                    break
    if cross_fixed_count == 0:
        print("v18.4+V18.5 meson_cross: files found but none required rewriting.")
else:
    print(f"v18.4 meson_cross.txt: NOT found under {BUILD_DIR}")
    # Diagnostic: list some shallow entries of BUILD_DIR so we see what's there
    try:
        entries = sorted(os.listdir(BUILD_DIR))[:40]
        print(f"v18.4   shallow listing of BUILD_DIR ({len(entries)} of {len(os.listdir(BUILD_DIR))} entries): {', '.join(entries)}")
    except OSError:
        pass
