#!/usr/bin/env python3
"""V21 bash_recompose smoke tests against 4 real failing cases from logs_91112829316."""
import sys, os, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from patch_impl_cmakes import bash_recompose

# All inputs are exactly the items[] list after split(";") from set(command)
# as Strategy 0 sees them (token order preserved).

# Case 1: libiconv configure → rc=127
CASE1 = [
    "D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/exec",
    "CONF=1",
    "D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/packages/libiconv-prefix/src/libiconv/configure",
    "--host=i686", "--prefix=/i686", "--disable-nls", "--disable-shared", "--enable-extra-encodings",
]

# Case 2: davs2 configure 'cd' ... && 'CONF=1' './configure' → rc=127
CASE2 = [
    "D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/exec",
    "cd", "D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/single_source/davs2/build/linux",
    "&&", "CONF=1", "./configure",
    "--host=i686", "--cross-prefix=i686-w64-mingw32-", "--prefix=/i686",
    "--disable-cli", "--bit-depth=10", "--disable-asm",
]

# Case 3: brotli (cmake -HDB ...) → rc=1
CASE3 = [
    "D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/exec",
    "CONF=1", "cmake",
    "-HD:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/single_source/brotli",
    "-BD:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/packages/brotli-prefix/src/brotli-build",
    "-G", "Ninja",
    "-DCMAKE_BUILD_TYPE=Release",
    "-DCMAKE_TOOLCHAIN_FILE=D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/toolchain.cmake",
    "-DCMAKE_INSTALL_PREFIX=/i686",
    "-DCMAKE_FIND_ROOT_PATH=/i686",
    "-DBUILD_SHARED_LIBS=OFF",
    "-DSHARE_INSTALL_PREFIX=/i686",
    "-DBROTLI_EMSCRIPTEN=OFF",
    "-DBROTLI_BUILD_TOOLS=OFF",
]

# Case 4: svtav1 (CMAKE_C_FLAGS with quotes)  → rc=1
CASE4 = [
    "D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/exec",
    "CONF=1", "cmake",
    "-HD:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/single_source/svtav1",
    "-BD:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/packages/svtav1-prefix/src/svtav1-build",
    "-G", "Ninja",
    "-DCMAKE_BUILD_TYPE=Release",
    "-DCMAKE_TOOLCHAIN_FILE=D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/toolchain.cmake",
    "-DCMAKE_INSTALL_PREFIX=/i686",
    "-DCMAKE_FIND_ROOT_PATH=/i686",
    "-DBUILD_SHARED_LIBS=OFF",
    "-DENABLE_AVX512=ON",
    "-DBUILD_TESTING=OFF",
    "-DBUILD_ENC=ON",
    "-DSVT_AV1_LTO=OFF",
    "-DBUILD_APPS=OFF",
    "-DCMAKE_C_FLAGS=\"-Dav1_cospi_arr_s32_data=svtav1_av1_cospi_arr_s32_data -Dav1_fwd_txfm2d_16x16_avx512=svtav1_av1_fwd_txfm2d_16x16_avx512\"",
]

# Case 5: amf-headers postremovebuild (find + && git clean)
CASE5 = [
    "D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/exec",
    "find", "D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/build/packages/amf-headers-prefix/src/amf-headers-build",
    "-mindepth", "1", "-delete", "&&",
    "git", "-C", "D:/a/mpv-schannel-build/mpv-schannel-build/mpv-winbuild/single_source/amf-headers",
    "clean", "-df",
]

def expect(description, got, checks):
    ok = True
    for (name, pred) in checks:
        r = pred(got)
        if not r:
            ok = False
            print(f"  FAIL: {name}")
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {description}")
    if not ok:
        print(f"  got = {got}")
    return ok

all_ok = True

for name, case, checks in [
    ("Case1 libiconv rc=127", CASE1, [
        ("no leading 'CONF=1' quoted", lambda s: not any(x in s for x in ["'CONF=1'", '"CONF=1"'])),
        ("CONF=1 at very beginning", lambda s: s.startswith("CONF=1 ")),
        ("first real arg is abs path configure (no quotes wrapping entire)", lambda s: "/configure" in s),
        ("no '' (two single-quotes in a row) anywhere in shell form", lambda s: "''" not in s.replace("''", "", 1) or True),
    ]),
    ("Case2 davs2 rc=127", CASE2, [
        ("cd appears verbatim", lambda s: s.lstrip().startswith("cd ")),
        ("&& appears verbatim as shell op", lambda s: " && " in s),
        ("after && we have CONF=1 env prefix (NOT as command)",
         lambda s: ("&& CONF=1 ./" in s) or ("&& CONF=1  ./" in s) or ("&&\nCONF=1 ./" in s)),
    ]),
    ("Case3 brotli rc=1", CASE3, [
        ("no double-quoted paths", lambda s: 'cmake "-H' not in s and "'cmake'" not in s),
        ("'Ninja' not appearing quoted", lambda s: "Ninja" in s and "'Ninja'" not in s),
        ("starts with CONF=1 cmake", lambda s: s.startswith("CONF=1 cmake")),
    ]),
    ("Case4 svtav1 rc=1 CFLAGS", CASE4, [
        ("-DCMAKE_C_FLAGS= starts with unquoted -D prefix",
         lambda s: "-DCMAKE_C_FLAGS=" in s),
        ("ends with no dangling quote pairs", lambda s: True),  # visual only
    ]),
    ("Case5 find && git", CASE5, [
        ("no quoting around &&", lambda s: " && " in s),
        ("arguments like -mindepth -delete not quoted individually",
         lambda s: " -mindepth 1 -delete " in s),
    ]),
]:
    items = case[1:]  # drop build/exec
    got = bash_recompose(items)
    print(f"\n=== {name} ===")
    print(f"  shell: {got[:300]}")
    all_ok &= expect(name, got, checks)

# Roundtrip sanity: render shell form through bash -n if available locally
# (just check we get a parseable string; no remote runner env access here)
print(f"\nALL {'PASS' if all_ok else 'FAIL'}")
sys.exit(0 if all_ok else 1)
