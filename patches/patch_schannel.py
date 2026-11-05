#!/usr/bin/env python3
"""
patch_schannel.py - Remove OpenSSL from shinchiro/mpv-winbuild-cmake and use
Windows Schannel TLS instead, AND make the build resilient to FFmpeg "option
drift".

OpenSSL -> Schannel changes (plain text replacements, verified one by one):

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

---------------------------------------------------------------------------
OPTION-DRIFT AUTO-TOLERANCE (the key robustness feature)
---------------------------------------------------------------------------
The superbuild is pinned to an OLDER commit (to keep a working ANGLE / mpv
build), but FFmpeg is auto-fetched as the LATEST release at build time. When a
newer FFmpeg removes an option the pinned superbuild still hardcodes (e.g.
`--enable-opengl`, dropped by FFmpeg in 2025), ffmpeg's configure used to die
with "Unknown option" and break the whole build.

We inject a `PATCH_COMMAND` into ffmpeg's ExternalProject that runs
`fix_ffmpeg_configure.sh` on the freshly downloaded FFmpeg source. That script
rewrites FFmpeg's `die_unknown()` so that UNKNOWN `--enable-X` / `--disable-X`
options are warned about and SKIPPED (return 0) instead of being fatal, while
EVERY OTHER unknown option still fails the build (so genuine mistakes still
surface).

Net effect: future FFmpeg releases that drop a hardcoded option can no longer
break the build. No more per-option patch maintenance - the drift is absorbed
automatically. The helper scripts are written into the superbuild root by this
patch and referenced via ${CMAKE_SOURCE_DIR} (always the top-level source dir,
independent of add_subdirectory nesting).

After patching, nothing in the mpv / mpv-release build DAG references the
openssl ExternalProject anymore, so libmpv-2.dll and mpv.exe link NO OpenSSL
code at all. TLS (https in mpv streams and libcurl) goes through Schannel,
i.e. Windows' own SChannel SSPI with the system certificate store.

WHY TLS 1.2/1.3 ONLY (fixes the Windows "insecure TLS" Program Compatibility
Assistant popup on live streams): FFmpeg's tls_schannel.c leaves
SCHANNEL_CRED.grbitEnabledProtocols at 0 (system default). On many Windows
clients that default STILL advertises TLS 1.0/1.1 in the ClientHello. Windows'
Program Compatibility Assistant hooks Schannel and pops "this program is trying
to use an insecure TLS version" whenever such a handshake is attempted -- which
is exactly why it only appears after a few stalls/reconnects (each fresh socket
re-opens a TLS session). We inject an awk transform (fix_tls_schannel.awk) via
the same ffmpeg PATCH_COMMAND that pins grbitEnabledProtocols to
SP_PROT_TLS1_2_CLIENT | SP_PROT_TLS1_3_CLIENT, removing the legacy offer
entirely. This is harmless: modern live-stream servers all speak TLS 1.2+, and
it does not affect DTLS (overridden by the existing DTLS block).

The SAME restriction is applied to curl's Schannel backend (lib/vtls/schannel.c)
for the same reason: curl's schannel.c computes grbitEnabledProtocols / the
SCH_CREDENTIALS grbitDisabledProtocols from the requested SSL version range, and
its DEFAULT range still advertises TLS 1.0/1.1 in the ClientHello. We inject
fix_curl_schannel.awk via curl's PATCH_COMMAND (after shinchiro's own git-am
patches) that forces TLS 1.2/1.3 only in BOTH the legacy SCHANNEL_CRED branch
(`schannel_cred.grbitEnabledProtocols`) and the modern SCH_CREDENTIALS branch
(`credentials.pTlsParameters->grbitDisabledProtocols`), so every curl https
handshake from mpv (playlist fetch, lua http, etc.) also avoids the PCA popup.

Usage:
    python3 patch_schannel.py <superbuild-root>   # e.g. mpv-winbuild/

Exits non-zero if any expected pattern is missing, so upstream layout
changes are caught immediately instead of silently producing an OpenSSL
build.
"""
import sys
import pathlib
import difflib

# ---------------------------------------------------------------------------
# Option-drift tolerance helper scripts. Written into the superbuild root at
# patch time, then run by ffmpeg's ExternalProject PATCH_COMMAND on the
# freshly downloaded FFmpeg source.
# ---------------------------------------------------------------------------
FIX_FFMPEG_SH = r'''#!/bin/bash
# (1) Option-drift safety net for FFmpeg's configure: rewrites die_unknown() so
#     unknown --enable-X / --disable-X options are warned and SKIPPED instead of
#     fatal. (2) Restricts Schannel's TLS to 1.2/1.3 so Windows' Program
#     Compatibility Assistant does not pop "insecure TLS" on live-stream
#     reconnects. See the two .awk helpers for the exact transforms.
set -u
SRCDIR="${1:-.}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- (1) configure option-drift tolerance ---
conf="$SRCDIR/configure"
if [ -f "$conf" ]; then
  if grep -q 'AUTO_TOLERANCE_PATCH' "$conf"; then
    echo "fix_ffmpeg_configure: configure already patched, skipping"
  else
    awk -f "$SCRIPT_DIR/fix_ffmpeg_configure.awk" "$conf" > "$conf.tmp" && mv "$conf.tmp" "$conf"
    # The redirected tmp file loses the original executable bit; ffmpeg's
    # configure is run directly (./configure) by ExternalProject, so restore +x.
    chmod +x "$conf"
    echo "fix_ffmpeg_configure: patched $conf for option-drift tolerance"
  fi
else
  echo "fix_ffmpeg_configure: $conf not found, skipping"
fi

# --- (2) Schannel TLS 1.2/1.3 restriction (avoids Windows PCA "insecure TLS") ---
tls_c="$SRCDIR/libavformat/tls_schannel.c"
if [ -f "$tls_c" ]; then
  if grep -q 'SCHANNEL_TLS12_ONLY' "$tls_c"; then
    echo "fix_ffmpeg_configure: tls_schannel.c already restricted, skipping"
  else
    awk -f "$SCRIPT_DIR/fix_tls_schannel.awk" "$tls_c" > "$tls_c.tmp" && mv "$tls_c.tmp" "$tls_c"
    if grep -q 'SCHANNEL_TLS12_ONLY' "$tls_c"; then
      echo "fix_ffmpeg_configure: restricted tls_schannel.c to TLS 1.2/1.3"
    else
      echo "fix_ffmpeg_configure: WARNING tls_schannel.c restriction did NOT apply (source may have drifted)"
    fi
  fi
else
  echo "fix_ffmpeg_configure: $tls_c not found, skipping"
fi
'''

# awk transform: replace the `die_unknown(){ ... }` function with a tolerant
# version. `dq` is a literal double-quote and `dl` a literal dollar sign, built
# via sprintf so we never fight shell/awk quoting and avoid escape warnings.
FIX_FFMPEG_AWK = r'''BEGIN { dq=sprintf("%c",34); dl=sprintf("%c",36) }
/^die_unknown\(\)\{$/ {
    print "die_unknown(){"
    print "    case " dq dl "1" dq " in"
    print "        --enable-*|--disable-*) echo " dq "WARNING: ignoring unknown enable/disable option (auto-tolerance)." dq "; return 0;;"
    print "    esac"
    print "    echo " dq "Unknown option (auto-tolerance: fatal path)." dq
    print "    exit 1"
    print "}"
    print "# AUTO_TOLERANCE_PATCH"
    skip=1
    next
}
skip==1 && /^}$/ { skip=0; next }
{ if (skip==0) print }
'''

# awk transform: in FFmpeg's libavformat/tls_schannel.c, the client branch of
# tls_open() only sets `schannel_cred.dwFlags |= SCH_CRED_NO_DEFAULT_CREDS;` and
# leaves grbitEnabledProtocols at 0 (system default). On many Windows clients that
# default still advertises TLS 1.0/1.1, and the Windows Program Compatibility
# Assistant pops "this program is trying to use an insecure TLS version" on any
# such handshake (notably after a live-stream stall/reconnect opens a fresh
# socket). We insert an explicit restriction to TLS 1.2/1.3 right after that
# dwFlags line. The later DTLS block overrides grbitEnabledProtocols for DTLS, so
# only ordinary TLS client connections are affected. The #ifdef guards against
# toolchains whose schannel.h lacks SP_PROT_TLS1_3_CLIENT.
FIX_TLS_SCHANNEL_AWK = r'''# Restrict Schannel to TLS 1.2/1.3 in FFmpeg's tls_schannel.c.
# See the header comment in patch_schannel.py (FIX_TLS_SCHANNEL_AWK) for why.
/schannel_cred\.dwFlags.*SCH_CRED_NO_DEFAULT_CREDS/ {
    print
    print "        /* SCHANNEL_TLS12_ONLY: restrict to TLS 1.2/1.3 (see fix_tls_schannel.awk). */"
    print "        schannel_cred.grbitEnabledProtocols = SP_PROT_TLS1_2_CLIENT"
    print "#ifdef SP_PROT_TLS1_3_CLIENT"
    print "            | SP_PROT_TLS1_3_CLIENT"
    print "#endif"
    print "            ;"
    next
}
{ print }
'''

# bash driver for the curl Schannel TLS 1.2/1.3 restriction. Written into the
# superbuild root; invoked from curl's PATCH_COMMAND (after shinchiro's own
# git-am patches) on the freshly downloaded curl source. Same shape as the
# ffmpeg driver: idempotent, drift-safe (prints WARNING + skips if the anchors
# are gone, never fails the build).
FIX_CURL_SH = r'''#!/bin/bash
# Restrict curl's Schannel to TLS 1.2/1.3 so Windows' Program Compatibility
# Assistant does not pop "insecure TLS" on any curl https handshake from mpv
# (playlist fetch, lua http, etc.). See fix_curl_schannel.awk for the transform.
set -u
SRCDIR="${1:-.}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sch="$SRCDIR/lib/vtls/schannel.c"
if [ -f "$sch" ]; then
  if grep -q 'CURL_SCHANNEL_TLS12_ONLY' "$sch"; then
    echo "fix_curl_schannel: already restricted, skipping"
  else
    awk -f "$SCRIPT_DIR/fix_curl_schannel.awk" "$sch" > "$sch.tmp" && mv "$sch.tmp" "$sch"
    if grep -q 'CURL_SCHANNEL_TLS12_ONLY' "$sch"; then
      echo "fix_curl_schannel: restricted $sch to TLS 1.2/1.3"
    else
      echo "fix_curl_schannel: WARNING restriction did NOT apply (curl schannel.c may have drifted)"
    fi
  fi
else
  echo "fix_curl_schannel: $sch not found, skipping"
fi
'''

# awk transform for curl's lib/vtls/schannel.c. Two exit points write the
# enabled/disabled protocol mask in acquire_sspi_handle():
#   (a) legacy branch:  schannel_cred.grbitEnabledProtocols = enabled_protocols;
#       -> keep the assignment, then AND it down to TLS 1.2/1.3 (incremental,
#          robust even if upstream tweaks the surrounding code).
#   (b) modern branch (Win11 / Server 2022+, no cipher_list):
#       credentials.pTlsParameters->grbitDisabledProtocols = ~enabled_protocols;
#       -> REPLACE with a fixed disabled mask that leaves only TLS 1.2/1.3 on.
# The #ifdef guards against toolchains whose schannel.h lacks SP_PROT_TLS1_3_CLIENT.
# If neither anchor matches (curl drifts), the file is left untouched.
FIX_CURL_SCHANNEL_AWK = r'''# Restrict curl's Schannel to TLS 1.2/1.3. See patch_schannel.py (FIX_CURL_SCHANNEL_AWK).
/schannel_cred\.grbitEnabledProtocols = enabled_protocols;/ {
    print
    print "    /* CURL_SCHANNEL_TLS12_ONLY: restrict to TLS 1.2/1.3 (see fix_curl_schannel.awk). */"
    print "    schannel_cred.grbitEnabledProtocols &= (SP_PROT_TLS1_2_CLIENT"
    print "#ifdef SP_PROT_TLS1_3_CLIENT"
    print "        | SP_PROT_TLS1_3_CLIENT"
    print "#endif"
    print "        );"
    next
}
/grbitDisabledProtocols = ~enabled_protocols;/ {
    print "    /* CURL_SCHANNEL_TLS12_ONLY: disable all but TLS 1.2/1.3 (see fix_curl_schannel.awk). */"
    print "    credentials.pTlsParameters->grbitDisabledProtocols = ~(SP_PROT_TLS1_2_CLIENT"
    print "#ifdef SP_PROT_TLS1_3_CLIENT"
    print "        | SP_PROT_TLS1_3_CLIENT"
    print "#endif"
    print "        );"
    next
}
{ print }
'''

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
    # Inject option-drift tolerance: run fix_ffmpeg_configure.sh as ffmpeg's
    # PATCH_COMMAND (executed by ExternalProject right after download, before
    # configure). ${CMAKE_SOURCE_DIR} is the superbuild root where this patch
    # wrote fix_ffmpeg_configure.sh / .awk.
    ("packages/ffmpeg.cmake",
     "    UPDATE_COMMAND \"\"\n",
     "    UPDATE_COMMAND \"\"\n"
     "    PATCH_COMMAND bash ${CMAKE_SOURCE_DIR}/fix_ffmpeg_configure.sh <SOURCE_DIR>\n",
     1),
    # ---------------- fontconfig: pin to a commit the superbuild's patches apply to ----------------
    # fontconfig is fetched with GIT_TAG main (moving target). shinchiro's two
    # patches (0001 Custom changes for mpv builds, 0002 Do not use dirent.h) are
    # ESSENTIAL for the Windows/i686 build (the _WIN32 SHGetFolderPathA font-dir
    # code and the dirent.h meson-drop). When fontconfig main drifts, `git am
    # --3way` can no longer apply the patches straight and falls back to a 3-way
    # merge, which needs the BASE blob of every touched file (e.g. the
    # `src/fcdir.c` blob c17875f referenced by the patch's `index` line).
    #
    # Two things make that 3-way merge fail on CI:
    #   (1) the superbuild clones with --filter=tree:0 (no historical trees), and
    #       even our global tree:0 -> blob:none relaxation is not enough on some
    #       runner git versions, which refuse to fetch the base blob on demand
    #       and die with "could not build fake ancestor" instead of falling back
    #       to a straight apply;
    #   (2) a stale build-cache checkout can leave the working tree on a drifted
    #       `main` rather than the pinned commit, so the straight apply also
    #       fails and 3-way is the only path.
    #
    # Fix (verified locally): pin to b25ffeabd15b (the exact base commit the two
    # patches were generated against - `git am --3way` applies straight on it with
    # rc=0) AND make fontconfig a FULL clone (drop the filter entirely) so the
    # 3-way base blob is always present, whichever path git takes. fontconfig is
    # a low-churn dependency; pinning it for reproducibility is the right call.
    # mpv/ffmpeg still auto-track latest.
    ("packages/fontconfig.cmake",
     "    GIT_TAG main\n",
     "    GIT_TAG b25ffeabd15b\n",
     1),
    # Full clone for fontconfig (see comment above). This REPLACEMENTS entry runs
    # BEFORE the global tree:0 -> blob:none loop, so fontconfig ends up with no
    # filter (full history) while every other git package keeps the lean
    # blob:none clone as a safety net.
    ("packages/fontconfig.cmake",
     "    GIT_CLONE_FLAGS \"--filter=tree:0\"\n",
     "    GIT_CLONE_FLAGS \"\"\n",
     1),
    # ---------------- superbuild git machinery: allow pinned commit hashes ----------------
    # shinchiro's helper (cmake/custom_steps.cmake) writes a `reset_head.sh`
    # that, for a package whose GIT_REMOTE_NAME is set (fontconfig sets
    # `GIT_REMOTE_NAME origin`) and whose GIT_TAG is a bare COMMIT HASH (what we
    # pin fontconfig to), computes `reset="@{u}"` and then runs, inside the
    # then-branch:
    #       git -C <src> reset --hard @{u} -q
    # A hash checkout is a DETACHED HEAD with no upstream, so `git reset --hard
    # @{u}` dies with "fatal: HEAD does not point to a branch" (exit 128) and
    # `set -e` aborts the whole fontconfig force-update / postremovebuild step
    # (this is the REAL failure - NOT the rev-parse @{u} in the [[ ]] guard,
    # which only prints a harmless stderr warning).
    #
    # Fix: make the then-branch reset tolerant of a detached HEAD. Try
    # `git reset --hard @{u}` first; if it fails (no upstream), fall back to
    # `git reset --hard -q` which simply stays at the already-checked-out pinned
    # commit. Both behaviours are correct: a real branch tracking @{u} moves to
    # upstream, a detached HEAD stays pinned.
    #
    # We also suppress the harmless stderr "fatal: HEAD does not point to a
    # branch" from the [[ ]] guard's `git rev-parse @{u}` so the log stays clean.
    ("cmake/custom_steps.cmake",
     "git -C ${source_dir} rev-parse @{u}",
     "git -C ${source_dir} rev-parse @{u} 2>/dev/null || true",
     1),
    ("cmake/custom_steps.cmake",
     "    git -C ${source_dir} reset --hard ${reset} -q\n",
     "    git -C ${source_dir} reset --hard ${reset} -q 2>/dev/null || git -C ${source_dir} reset --hard -q\n",
     1),
    # The force-update step hardcodes `git fetch --filter=tree:0`. If it ever
    # runs (or a stale cache re-enters the flow), re-partializing the repo would
    # again strip the 3-way base trees/blobs and re-trigger "could not build fake
    # ancestor". Drop the filter so any fetch stays a normal full fetch.
    ("cmake/custom_steps.cmake",
     "git fetch --filter=tree:0 --no-recurse-submodules || true",
     "git fetch --no-recurse-submodules || true",
     1),
    # ---------------- libvpl: let git am --3way fetch the 3-way base ----------------
    # libvpl tracks GIT_TAG main (a moving branch) and is patched with
    # `git am --3way`. When upstream drifts, the patch no longer applies
    # directly and git falls back to a 3-way merge, which needs the BASE blob
    # of the file. The superbuild clones with --filter=tree:0 (no historical
    # blobs/trees), so the base is absent and `git am --3way` dies with
    #   "sha1 information is lacking or useless" / "could not build fake ancestor"
    # We do a FULL clone for libvpl (drop the filter) so the base blob is
    # present and the drifted patch applies via 3-way. libvpl's history is
    # modest, so a full clone is affordable (and cached afterwards).
    ("packages/libvpl.cmake",
     "    GIT_CLONE_FLAGS \"--filter=tree:0\"\n",
     "    GIT_CLONE_FLAGS \"\"\n",
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
    # Inject the Schannel TLS 1.2/1.3 restriction into curl's source. curl's
    # PATCH_COMMAND already runs shinchiro's own `git am --3way`; we chain our
    # awk driver after it. ${CMAKE_SOURCE_DIR} is the superbuild root where this
    # patch wrote fix_curl_schannel.sh / .awk, and <SOURCE_DIR> is the freshly
    # cloned curl source.
    ("packages/curl.cmake",
     "    PATCH_COMMAND ${EXEC} git am --3way ${CMAKE_CURRENT_SOURCE_DIR}/curl-*.patch\n",
     "    PATCH_COMMAND ${EXEC} git am --3way ${CMAKE_CURRENT_SOURCE_DIR}/curl-*.patch && bash ${CMAKE_SOURCE_DIR}/fix_curl_schannel.sh <SOURCE_DIR>\n",
     1),
    # curl's PATCH_COMMAND also uses `git am --3way`. Give it a FULL clone too
    # (same reason as fontconfig/libvpl): if a cached partial clone ever has to
    # fall back to a 3-way merge, the base blobs MUST be present or git dies
    # with "could not build fake ancestor". curl's history is modest, and the
    # repo is cached afterwards. This REPLACEMENTS entry runs before the global
    # tree:0 -> blob:none loop, so curl keeps the empty (full-clone) flags.
    ("packages/curl.cmake",
     "    GIT_CLONE_FLAGS \"--filter=tree:0\"\n",
     "    GIT_CLONE_FLAGS \"\"\n",
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

    # ------------------------------------------------------------------
    # Global fix: --filter=tree:0 (partial clone) breaks `git am --3way` for
    # ANY git package whose upstream has drifted since this superbuild commit
    # (05a60b3cfd) was pinned. With tree:0 the 3-way merge BASE tree/blob is
    # absent, so drifted patches fail with "could not build fake ancestor".
    # Switching to --filter=blob:none keeps clones lean (blobs still fetched
    # on demand) while trees ARE present, so git am --3way can locate the base
    # and reconstruct the ancestor. This is a safety net for every git package
    # that tracks a moving branch; libvpl and fontconfig above are switched to
    # a FULL clone for an extra guarantee on the packages that have actually
    # failed this way.
    #
    # NOTE: the real flag strings in the superbuild are often prefixed, e.g.
    # "--sparse --filter=tree:0" or "--depth=1 --filter=tree:0". We therefore
    # match the bare `--filter=tree:0` SUBSTRING (no surrounding quotes) and
    # replace only that token, keeping any --sparse/--depth=1 prefix intact.
    GLOBAL_FILTER_OLD = '--filter=tree:0'
    GLOBAL_FILTER_NEW = '--filter=blob:none'
    for cmake_file in sorted(root.rglob("*.cmake")):
        try:
            t = cmake_file.read_text(encoding="utf-8")
        except (IsADirectoryError, UnicodeDecodeError, PermissionError):
            continue
        if GLOBAL_FILTER_OLD in t:
            new_t = t.replace(GLOBAL_FILTER_OLD, GLOBAL_FILTER_NEW)
            cmake_file.write_text(new_t, encoding="utf-8")
            n = t.count(GLOBAL_FILTER_OLD)
            print(f"OK   [global] {cmake_file}: --filter=tree:0 -> blob:none"
                  f" ({n} occurrence{'s' if n != 1 else ''})")

    # Write the option-drift tolerance + Schannel TLS 1.2/1.3 helper scripts
    # into the superbuild root (referenced by ffmpeg's / curl's PATCH_COMMAND
    # via ${CMAKE_SOURCE_DIR}).
    (root / "fix_ffmpeg_configure.sh").write_text(FIX_FFMPEG_SH, encoding="utf-8")
    (root / "fix_ffmpeg_configure.awk").write_text(FIX_FFMPEG_AWK, encoding="utf-8")
    (root / "fix_tls_schannel.awk").write_text(FIX_TLS_SCHANNEL_AWK, encoding="utf-8")
    (root / "fix_curl_schannel.sh").write_text(FIX_CURL_SH, encoding="utf-8")
    (root / "fix_curl_schannel.awk").write_text(FIX_CURL_SCHANNEL_AWK, encoding="utf-8")
    print("OK   wrote fix_ffmpeg_configure.sh / fix_ffmpeg_configure.awk "
          "(option-drift tolerance), fix_tls_schannel.awk (ffmpeg TLS 1.2/1.3), "
          "and fix_curl_schannel.sh / fix_curl_schannel.awk (curl TLS 1.2/1.3)")

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
