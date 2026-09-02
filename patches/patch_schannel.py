#!/usr/bin/env python3
"""
patch_schannel.py - Remove OpenSSL from shinchiro/mpv-winbuild-cmake and use
Windows Schannel TLS instead.

What it does (all changes are plain text replacements, verified one by one):

  packages/ffmpeg.cmake
      - DEPENDS: drop `openssl`, `libssh`, `libsrt`, `libaribcaption`
        (libssh/libsrt/libaribcaption all pull in openssl; dropping them also
         drops ffmpeg's ssh/srt protocols and the ARIB caption decoder -
         none of which matter for DVD/Blu-ray/https playback)
      - CONFIGURE_COMMAND: `--enable-openssl` -> `--enable-schannel`
        and remove `--enable-libssh`, `--enable-libsrt`, `--enable-libaribcaption`
        ffmpeg's tls-schannel backend needs only mingw-w64 headers +
        -lsecur32/-lcrypt32, both linked automatically by ffmpeg configure.

  packages/curl.cmake
      - DEPENDS: drop `openssl`, `libssh`, `ngtcp2`, `nghttp3`
        (ngtcp2/nghttp3 are HTTP/3-over-QUIC libs that require openssl)
      - CURL_USE_OPENSSL=OFF, CURL_USE_SCHANNEL=ON  (USE_WINDOWS_SSPI is
        already ON upstream, which is exactly what Schannel needs)
      - CURL_USE_LIBSSH=OFF, USE_NGHTTP3=OFF, USE_NGTCP2=OFF, USE_ECH=OFF,
        USE_PROXY_HTTP3=OFF (ECH and HTTP/3 are openssl-only features)
      - drop -DNGHTTP3_STATICLIB/-DNGTCP2_STATICLIB from CMAKE_C_FLAGS,
        keep -lcrypt32 -lsecur32 (required by Schannel)

  packages/libarchive.cmake
      - DEPENDS: drop `openssl`
      - ENABLE_OPENSSL=OFF (zip/LZMA crypto falls back to Windows Crypto API)

  packages/mpv.cmake  AND  packages/mpv-release.cmake
      - DEPENDS: drop `subrandr` and `vapoursynth`
        * subrandr: upstream build script hard-panics on i686-pc-windows-gnu
          ("Building for i686-pc-windows-gnu is currently known to be broken!",
          issue afishhh/subrandr#31). It is an X11 RandR substitute, unused
          on native Windows. The stable mpv release (v0.41.0) has NO
          `subrandr` meson option, so the `-Dsubrandr=enabled` line must be
          REMOVED entirely (setting it to `=disabled` is rejected as unknown).
        * vapoursynth: its 32-bit (i686) import lib does not export the
          stdcall-mangled `getVSScriptAPI@4` symbol, so linking mpv.exe /
          libmpv-2.dll fails with "undefined reference to _imp__getVSScriptAPI@4".
          VapourSynth is an advanced scripting-filter feature, not needed for
          DVD / video playback, so we disable it for this 32-bit build.

After patching, nothing in the mpv / mpv-release build DAG references the
openssl ExternalProject anymore, so libmpv-2.dll and mpv.exe link NO OpenSSL
code at all. TLS (https in mpv streams and libcurl) goes through Schannel,
i.e. Windows' own SChannel SSPI with the system certificate store.

Usage:
    python3 patch_schannel.py <superbuild-root>   # e.g. mpv-winbuild/

Exits non-zero if any expected pattern is missing, so upstream layout
changes are caught immediately instead of silently producing an OpenSSL
build.
"""
import sys
import pathlib
import difflib

# (file, old, new, expected_count)
REPLACEMENTS = [
    # ---------------- ffmpeg ----------------
    ("packages/ffmpeg.cmake",
     "        openssl\n",
     "",
     1),
    ("packages/ffmpeg.cmake",
     "        libssh\n",
     "",
     1),
    ("packages/ffmpeg.cmake",
     "        libsrt\n",
     "",
     1),
    ("packages/ffmpeg.cmake",
     "        libaribcaption\n",
     "",
     1),
    ("packages/ffmpeg.cmake",
     "        --enable-openssl\n",
     "        --enable-schannel\n",
     1),
    ("packages/ffmpeg.cmake",
     "        --enable-libssh\n",
     "",
     1),
    ("packages/ffmpeg.cmake",
     "        --enable-libsrt\n",
     "",
     1),
    ("packages/ffmpeg.cmake",
     "        --enable-libaribcaption\n",
     "",
     1),
    # ---------------- curl ----------------
    ("packages/curl.cmake",
     "        openssl\n",
     "",
     1),
    ("packages/curl.cmake",
     "        libssh\n",
     "",
     1),
    ("packages/curl.cmake",
     "        ngtcp2\n",
     "",
     1),
    ("packages/curl.cmake",
     "        nghttp3\n",
     "",
     1),
    ("packages/curl.cmake",
     "        -DCURL_USE_LIBSSH=ON\n",
     "        -DCURL_USE_LIBSSH=OFF\n",
     1),
    ("packages/curl.cmake",
     "        -DCURL_USE_OPENSSL=ON\n",
     "        -DCURL_USE_OPENSSL=OFF\n"
     "        -DCURL_USE_SCHANNEL=ON\n",
     1),
    ("packages/curl.cmake",
     "        -DUSE_NGHTTP3=ON\n",
     "        -DUSE_NGHTTP3=OFF\n",
     1),
    ("packages/curl.cmake",
     "        -DUSE_NGTCP2=ON\n",
     "        -DUSE_NGTCP2=OFF\n",
     1),
    ("packages/curl.cmake",
     "        -DUSE_ECH=ON\n",
     "        -DUSE_ECH=OFF\n",
     1),
    ("packages/curl.cmake",
     "        -DUSE_PROXY_HTTP3=ON\n",
     "        -DUSE_PROXY_HTTP3=OFF\n",
     1),
    ("packages/curl.cmake",
     "'-DNGHTTP3_STATICLIB -DNGHTTP2_STATICLIB -DNGTCP2_STATICLIB -lz",
     "'-DNGHTTP2_STATICLIB -lz",
     1),
    # ---------------- libarchive ----------------
    ("packages/libarchive.cmake",
     "        openssl\n",
     "",
     1),
    ("packages/libarchive.cmake",
     "        -DENABLE_OPENSSL=ON\n",
     "        -DENABLE_OPENSSL=OFF\n",
     1),
    # ---------------- mpv (git master) & mpv-release: drop subrandr ----------------
    # subrandr refuses to build for i686-pc-windows-gnu (upstream hard panic,
    # issue afishhh/subrandr#31). It is X11-only and unused on native Windows.
    # The stable mpv release (v0.41.0) has no `subrandr` meson option, so we
    # must REMOVE the `-Dsubrandr=enabled` line entirely (not set it to
    # "disabled", which would be rejected as an unknown option). With subrandr
    # gone from DEPENDS, mpv auto-builds without it.
    ("packages/mpv-release.cmake",
     "        subrandr\n",
     "",
     1),
    ("packages/mpv-release.cmake",
     "        -Dsubrandr=enabled\n",
     "",
     1),
    ("packages/mpv.cmake",
     "        subrandr\n",
     "",
     1),
    ("packages/mpv.cmake",
     "        -Dsubrandr=enabled\n",
     "",
     1),
    # vapoursynth: 32-bit import lib lacks getVSScriptAPI@4 -> link fails.
    # Disable it for this 32-bit build (advanced scripting filter, not needed).
    ("packages/mpv-release.cmake",
     "        vapoursynth\n",
     "",
     1),
    ("packages/mpv-release.cmake",
     "        -Dvapoursynth=enabled\n",
     "        -Dvapoursynth=disabled\n",
     1),
    ("packages/mpv.cmake",
     "        vapoursynth\n",
     "",
     1),
    ("packages/mpv.cmake",
     "        -Dvapoursynth=enabled\n",
     "        -Dvapoursynth=disabled\n",
     1),
]

# Files where, after patching, no reference to the dropped packages / openssl
# is allowed to remain (DEPENDS-wise). We only check standalone DEPENDS lines
# so flags like --disable-openssl elsewhere are fine.
FORBIDDEN_DEPEND_LINES = [
    ("packages/ffmpeg.cmake", "openssl"),
    ("packages/ffmpeg.cmake", "libssh"),
    ("packages/ffmpeg.cmake", "libsrt"),
    ("packages/ffmpeg.cmake", "libaribcaption"),
    ("packages/curl.cmake", "openssl"),
    ("packages/curl.cmake", "libssh"),
    ("packages/curl.cmake", "ngtcp2"),
    ("packages/curl.cmake", "nghttp3"),
    ("packages/libarchive.cmake", "openssl"),
    # nothing else in the mpv DAG may gain an openssl dep
    ("packages/mpv.cmake", "openssl"),
    ("packages/mpv-release.cmake", "openssl"),
    # subrandr must be gone from the mpv build (X11-only, broken on i686)
    ("packages/mpv.cmake", "subrandr"),
    ("packages/mpv-release.cmake", "subrandr"),
    # vapoursynth must be gone from the mpv build (i686 link failure)
    ("packages/mpv.cmake", "vapoursynth"),
    ("packages/mpv-release.cmake", "vapoursynth"),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = pathlib.Path(sys.argv[1])
    if not (root / "packages" / "ffmpeg.cmake").is_file():
        print(f"ERROR: {root} does not look like the mpv-winbuild-cmake root",
              file=sys.stderr)
        return 2

    originals = {}

    def load(rel: str) -> str:
        if rel not in originals:
            originals[rel] = (root / rel).read_text(encoding="utf-8")
        return originals[rel]

    diffs = []
    failed = False

    for rel, old, new, expected in REPLACEMENTS:
        text = load(rel)
        count = text.count(old)
        if count != expected:
            print(f"ERROR [{rel}] pattern count {count} != {expected}:\n"
                  f"  {old!r}", file=sys.stderr)
            failed = True
            continue
        load(rel)  # ensure loaded
        originals[rel] = text.replace(old, new)
        print(f"OK   [{rel}] {old.strip()!r} -> {new.strip()!r}")

    if failed:
        return 1

    for rel, text in originals.items():
        new_text = text
        if new_text == (root / rel).read_text(encoding="utf-8"):
            continue
        diff = difflib.unified_diff(
            (root / rel).read_text(encoding="utf-8").splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}")
        diffs.append("".join(diff))
        (root / rel).write_text(new_text, encoding="utf-8")

    # Post-checks: no leftover dependency lines / openssl linkage in patched
    # files or in the mpv package definitions.
    problems = []
    for rel, needle in FORBIDDEN_DEPEND_LINES:
        for line in (root / rel).read_text(encoding="utf-8").splitlines():
            if line.strip() == needle:
                problems.append(f"{rel}: dependency line {needle!r} still present")
    if problems:
        for p in problems:
            print(f"ERROR post-check: {p}", file=sys.stderr)
        return 1

    # Global sanity: in every package file that belongs to the mpv DAG,
    # no line should be exactly "openssl" (a DEPENDS entry).
    for pkg in (root / "packages").glob("*.cmake"):
        for line in pkg.read_text(encoding="utf-8").splitlines():
            if line.strip() == "openssl" and pkg.name not in (
                    "openssl.cmake", "libssh.cmake", "libsrt.cmake",
                    "ngtcp2.cmake", "libaribcaption.cmake", "megasdk.cmake",
                    "curl.cmake", "libarchive.cmake", "ffmpeg.cmake"):
                print(f"WARN {pkg.name}: still depends on openssl "
                      "(not in mpv DAG? OK to ignore)")

    print("\n================ DIFF ================")
    for d in diffs:
        print(d)
    print("patch_schannel.py: all patches applied successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
