#!/usr/bin/env python3
"""
patch_build_ninja.py  —  v16 NINJA-DAG SURGERY

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

v16.2 fixes:
  - Fix is_rescue_step: use basename comparison instead of endswith
    (path ends with /mpv-build, not -mpv-build)
  - Add copy-package-dir, fullclean, liteclean, delete-dir to
    SKIP_VIA_TOUCH_STEPS (these delete build dirs or fail on impl.cmake)

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

# V16: UNIVERSAL skip steps — these steps are patched for ALL packages
# (not just whitelisted ones) because they cause build directory deletion
# which loses our compiled DLLs before copy-binary can run.
# - removebuild: deletes the package's build/ directory (loses .dll/.exe)
# - postremovebuild: post-cleanup after removebuild
UNIVERSAL_SKIP_STEPS = ["removebuild", "postremovebuild"]

# V16.1: Steps to skip via touch (non-whitelisted but cause failures)
# mpv-copy-binary fails because mpv-package/ dir doesn't exist.
# mpv-copy-package-dir fails because impl.cmake has shell commands.
# fullclean/liteclean/delete-dir may delete build dirs with our DLLs.
SKIP_VIA_TOUCH_STEPS = ["copy-binary", "copy-package-dir", "fullclean", "liteclean", "delete-dir"]

# V16.1: After these build steps complete, immediately copy DLL to rescue dir.
# Key insight: mpv-build generates libmpv-2.dll, but some later step may
# delete it before the rescue monitor's 1-second polling interval catches it.
# By appending cp directly to the build COMMAND, we copy the DLL the instant
# it's created.
RESCUE_AFTER_BUILD = {
    "mpv-build": {
        "dll": "packages/mpv-prefix/src/mpv-build/libmpv-2.dll",
        "exe": "packages/mpv-prefix/src/mpv-build/mpv.exe",
        "impla": "packages/mpv-prefix/src/mpv-build/libmpv.dll.a",
    },
    "mpv-release-build": {
        "dll": "packages/mpv-release-prefix/src/mpv-release-build/libmpv-2.dll",
        "exe": "packages/mpv-release-prefix/src/mpv-release-build/mpv.exe",
        "impla": "packages/mpv-release-prefix/src/mpv-release-build/libmpv.dll.a",
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

def is_whitelisted_output(outputs):
    """Return True if ANY output matches a whitelisted package pattern,
    OR if ANY output ends with a UNIVERSAL_SKIP_STEP (e.g. -removebuild),
    OR if ANY output ends with a SKIP_VIA_TOUCH_STEP (e.g. -copy-binary)."""
    for out in outputs:
        out_norm = out.replace('\\', '/')
        # V16: check whitelist packages
        for pkg, regex in PKG_REGEXES:
            if regex.search(out_norm):
                return True
        # V16: check universal skip steps (removebuild, postremovebuild)
        for step in UNIVERSAL_SKIP_STEPS:
            if out_norm.endswith('-' + step):
                return True
        # V16.1: check skip-via-touch steps (copy-binary)
        for step in SKIP_VIA_TOUCH_STEPS:
            if out_norm.endswith('-' + step):
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

def make_rescue_command(step_name):
    """Build a cp chain that copies DLL + mpv.exe + libmpv.dll.a to rescue/."""
    files = RESCUE_AFTER_BUILD[step_name]
    cmds = ['mkdir -p rescue']
    dest_map = {'dll': 'rescue/libmpv-2.dll', 'exe': 'rescue/mpv.exe', 'impla': 'rescue/libmpv.dll.a'}
    for key in ('dll', 'exe', 'impla'):
        path = files[key]
        dest = dest_map[key]
        cmds.append(f'cp -f "{path}" "{dest}" 2>/dev/null || true')
    return ' && '.join(cmds)

def make_touch_command(outputs):
    """Build a `cmake -E touch` chain for all outputs."""
    touches = []
    for out in outputs:
        out_path = out.replace('\\', '/')
        touches.append(f'cmake -E touch "{out_path}"')
    return ' && '.join(touches)

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
            rescue_cmd = make_rescue_command(rescue_step)
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
                    # Append rescue cp to the last line of COMMAND
                    # Replace trailing $ on last line, then append our cp
                    last_line = cmd_lines[-1]
                    if last_line.rstrip().endswith('$'):
                        # Remove $ and append rescue_cmd
                        last_line = last_line.rstrip()[:-1]  # remove $
                        last_line = last_line + f' && {rescue_cmd}\n'
                    else:
                        # Single line COMMAND — append before newline
                        last_line = last_line.rstrip('\n') + f' && {rescue_cmd}\n'
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

print(f"v16.2 patch_build_ninja.py: patched {patched} build rule COMMANDs -> cmake -E touch chain")
print(f"v16.2 patch_build_ninja.py: RERUN_CMAKE disabled = {rerun_patched}")
print(f"v16.2 patch_build_ninja.py: file = {NINJA_FILE}")
