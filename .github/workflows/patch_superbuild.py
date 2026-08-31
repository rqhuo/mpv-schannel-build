#!/usr/bin/env python3
"""
Run inside msys2 mingw32 shell (python interpreter is mingw-w64-i686-python),
after shinchiro mpv-winbuild-cmake superbuild has been cloned into CWD == repo root.

Purpose (v12.2):
  1. mpv-release.cmake:
       - Strip the execute_process(curl GitHub API → LINK) / URL ${LINK} dance.
         Rationale: on GHA runners curl often hits GitHub API rate-limit, LINK ends up
         empty, and ExternalProject_Add then fails with:
             "No download info given for 'mpv-release' and its source directory ..."
       - Instead we rely on the OFFICIAL superbuild mechanism:
             packages/CMakeLists.txt:112-114
                 if(NOT ${SINGLE_SOURCE_LOCATION} STREQUAL "")
                     set(SOURCE_LOCATION "${SINGLE_SOURCE_LOCATION}/${package}")
                 endif()
         So the caller clones mpv source into
             ${SINGLE_SOURCE_LOCATION}/mpv-release/   and   .../mpv/
         and passes -DSINGLE_SOURCE_LOCATION=<parent> to the top-level cmake.
         mpv-release.cmake line 43  SOURCE_DIR ${SOURCE_LOCATION}   then matches.
       - Add DOWNLOAD_COMMAND "" / UPDATE_COMMAND "" / PATCH_COMMAND "" because
         the patched ExternalProject_Add still had `URL ${LINK}` (now removed) and
         ExternalProject likes to re-check download props.
       - Append meson options:  -Dopenssl=disabled   -Dtls-backend=schannel

  2. mpv.cmake (the *non*-release EP; packages/CMakeLists.txt L96 also includes it):
       - Same meson-option injection (harmless if not used), so it also never
         accidentally drags in openssl linkage if someone enables the `mpv` EP.

  3. packages/ffmpeg.cmake:
       - Append --disable-openssl --enable-schannel --disable-gnutls
               --disable-mbedtls --disable-libtls
         right before BUILD_COMMAND / INSTALL_COMMAND / LOG_* lines so the flags
         are always part of ffmpeg's configure invocation.
       - Fallback: s/--enable-openssl -> --disable-openssl --enable-schannel,
         etc., in case ffmpeg.cmake's upstream someday changes the order/format.

  4. Glob every *.cmake / CMakeLists.txt under packages/, cmake/, ./** and append
     list-APPEND fallback snippets for common FFMPEG_* / MPV_* cache variable names.
     (Third layer of defence in case a future superbuild refactor moves option names.)

Args:
  sys.argv[1]  SB                 superbuild root (we chdir() here first)
  sys.argv[2]  PARENT_WIN         unused, just echoed for log traceability
  sys.argv[3]  MPV_RELEASE_DIR    unused, just echoed for log traceability
  sys.argv[4]  MPV_MPV_DIR        unused, just echoed for log traceability
"""
import sys, os, re, pathlib, glob, textwrap


def banner(title: str) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n{title}\n{bar}\n")


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def patch_mpv_release(text: str) -> str:
    # ---- (1a) Yank the whole get_latest_tag.sh generation block.
    #         Lines in the original look like:
    #             file(WRITE ${PREFIX_DIR}/get_latest_tag.sh "...")
    #             file(COPY ${PREFIX_DIR}/get_latest_tag.sh  DESTINATION ... FILE_PERMISSIONS ...)
    #             execute_process(COMMAND ${PREFIX_DIR}/src/get_latest_tag.sh
    #                             OUTPUT_VARIABLE LINK)
    #         Strategy:
    #           1) delete any file(WRITE ... get_latest_tag.sh ...) block (paren-matched by
    #              finding its closing ")" on its own or following lines)
    #           2) delete file(COPY ... get_latest_tag.sh ...) block
    #           3) delete the execute_process( ... OUTPUT_VARIABLE LINK ) block
    #         We do one regex: from first "file(WRITE ...get_latest_tag" through "...OUTPUT_VARIABLE LINK)"
    #         inclusive, because they always appear in order and contiguously in upstream's file.
    #
    #         Note: re.S => "." matches newlines too.  The non-greedy .*? stops at the first
    #               OUTPUT_VARIABLE LINK ) it sees, which is exactly the block we want.
    out = re.sub(
        r"(?ms)^file\s*\(\s*WRITE\s+\$\{PREFIX_DIR\}/get_latest_tag\.sh\b"
        r".*?"
        r"execute_process\s*\([^)]*\bOUTPUT_VARIABLE\s+LINK\b[^)]*\)\s*\n",
        "# --- [v12.2 CI] get_latest_tag / curl-GitHub-API / URL=${LINK} block disabled\n"
        "# --- [v12.2 CI] using -DSINGLE_SOURCE_LOCATION instead (see top-level cmake)\n",
        text,
    )
    # Secondary cleanup: sometimes the previous regex covers the block fine but a second
    # file(COPY ... get_latest_tag.sh ...) line sits just before WRITE if upstream reorders.
    # Kill any stray get_latest_tag lines regardless.
    out = re.sub(
        r"(?m)^\s*file\s*\(\s*COPY\s+[^)]*get_latest_tag\.sh[^)]*\)\s*\n",
        "",
        out,
    )

    # ---- (1b) Remove any remaining `URL ${LINK}` line
    out = re.sub(r"(?m)^\s*URL\s+\$\{LINK\}\s*\n", "", out)

    # ---- (1c) After the SOURCE_DIR line, explicitly set no-download steps.
    #           (ExternalProject gets nervous when both URL is missing AND it hasn't
    #            been told not to try downloading.)
    def _inject_download_empty(m):
        indent = m.group(2)  # whatever whitespace prefix SOURCE_DIR itself used
        src_line = m.group(1)
        extras = (
            f'{indent}DOWNLOAD_COMMAND ""\n'
            f'{indent}UPDATE_COMMAND ""\n'
            f'{indent}PATCH_COMMAND ""\n'
        )
        return src_line + extras

    # Preferred anchor: the superbuild-standard line `SOURCE_DIR ${SOURCE_LOCATION}`
    if re.search(r"(?m)^\s*SOURCE_DIR\s+\$\{SOURCE_LOCATION\}", out):
        out = re.sub(
            r"(?m)(^(\s*)SOURCE_DIR\s+\$\{SOURCE_LOCATION\}\s*\n)",
            _inject_download_empty,
            out,
        )
    else:
        # Fallback anchor: any SOURCE_DIR line whatsoever
        m_src = re.search(r"(?m)^(\s*)SOURCE_DIR\s+\S", out)
        if m_src:
            out = re.sub(
                r"(?m)(^(\s*)SOURCE_DIR\s+\S.*\n)",
                _inject_download_empty,
                out,
                count=1,
            )
        else:
            # Final fallback: inject SOURCE_DIR + DOWNLOAD_COMMAND right after the
            # ExternalProject_Add(mpv-release opening line, using the same indent
            # style as CONFIGURE_COMMAND if we can find it.
            indent = "    "
            m_cfg = re.search(r"(?m)^([ \t]*)CONFIGURE_COMMAND\b", out)
            if m_cfg:
                indent = m_cfg.group(1)
            inject = (
                f"{indent}SOURCE_DIR ${{SOURCE_LOCATION}}\n"
                f"{indent}DOWNLOAD_COMMAND \"\"\n"
                f"{indent}UPDATE_COMMAND \"\"\n"
                f"{indent}PATCH_COMMAND \"\"\n"
            )
            out = re.sub(
                r"(?m)(^[ \t]*ExternalProject_Add\s*\(\s*mpv-release\b.*\n)",
                lambda m: m.group(1) + inject,
                out,
                count=1,
            )

    # ---- (1d) Append meson no-openssl + schannel options.
    #           mpv-release.cmake's CONFIGURE_COMMAND ends with:
    #               -Dc_args='-Wno-error=int-conversion'
    #           so inject right after that line for reliability.
    if "-Dc_args='-Wno-error=int-conversion'" in out:
        out = out.replace(
            "-Dc_args='-Wno-error=int-conversion'",
            "-Dc_args='-Wno-error=int-conversion'\n"
            "        -Dopenssl=disabled\n"
            "        -Dtls-backend=schannel",
        )
    else:
        # Fallback: piggy-back on any line setting -Dlua=... (usually there)
        out = re.sub(
            r"(-Dlua=[A-Za-z0-9_.-]*)",
            r"\1 -Dopenssl=disabled -Dtls-backend=schannel",
            out,
        )
    return out


def patch_mpv(text: str) -> str:
    """Same meson-option injection for the non-release `mpv` EP file."""
    if "-Dc_args='-Wno-error=int-conversion'" in text:
        text = text.replace(
            "-Dc_args='-Wno-error=int-conversion'",
            "-Dc_args='-Wno-error=int-conversion'\n"
            "        -Dopenssl=disabled\n"
            "        -Dtls-backend=schannel",
        )
    # One more fallback even if c_args changed
    text = re.sub(
        r"(-Dlua=[A-Za-z0-9_.-]*)",
        r"\1 -Dopenssl=disabled -Dtls-backend=schannel",
        text,
    )
    # Also drop --enable-openssl / --enable-gnutls / etc if they ever appear
    # in mpv.cmake (mpv itself uses meson not ffmpeg-style configure, but be safe).
    text = (
        text.replace("--enable-openssl", "--disable-openssl --enable-schannel")
            .replace("--enable-gnutls",  "--disable-gnutls")
            .replace("--enable-mbedtls", "--disable-mbedtls")
            .replace("--enable-libtls",  "--disable-libtls")
    )
    return text


def patch_ffmpeg(text: str) -> str:
    """Append no-openssl/schannel flags into ffmpeg CONFIGURE_COMMAND block."""
    flags = "--disable-openssl --enable-schannel --disable-gnutls --disable-mbedtls --disable-libtls"
    # regex: capture everything from CONFIGURE_COMMAND … up to (but not including)
    # the next BUILD_COMMAND / INSTALL_COMMAND / LOG_* keyword on its own line.
    out = re.sub(
        r"(?ms)(CONFIGURE_COMMAND\s+.*?)(\n\s*(BUILD_COMMAND|INSTALL_COMMAND|LOG_[A-Z_]+)\b)",
        lambda m: m.group(1) + " " + flags + m.group(2),
        text,
    )

    # Aggressive fallbacks if upstream ever changes the CONFIGURE_COMMAND layout.
    out = (
        out.replace("--enable-openssl", "--disable-openssl --enable-schannel")
           .replace("--enable-gnutls",  "--disable-gnutls")
           .replace("--enable-mbedtls", "--disable-mbedtls")
           .replace("--enable-libtls",  "--disable-libtls")
    )
    return out


# List of ExternalProject step COMMANDs that (in shinchiro upstream impl.cmake)
# run shell-compound statements / MSYS2-only tools / assignments via `cd ... &&`
# or `VAR=value program`.  These commands expect the *invoking* process to be a
# bash-compatible shell; when CMake's execute_process(COMMAND ...) runs them via
# Windows CreateProcess they become "Command failed: 1" because the first argv
# token (cd / CONF=1 / make / rm / mv) is not a Win32 PE image.
#
# Fix (v12.4): for each ExternalProject_Add() call we find, replace the empty
# (implicit-default) step command for these known steps with a no-op "" **on
# the CMake definition side**, BEFORE CMake generates the --impl.cmake files.
# This way ExternalProject never emits an impl.cmake execute_process(COMMAND)
# for them at all, so execute_process(COMMAND make clean / rm -rf) is gone.
EP_STEP_COMMANDS_TO_NOOP = (
    # CMake ExternalProject standard step names.  Any of these that are *not*
    # explicitly set in packages/*.cmake will fall through to a default action,
    # and the defaults (UPDATE_DISCONNECTED 1 → git, REMOVE build dir via shell,
    # etc.) are the ones that hit shell-only commands.
    #
    # We intentionally skip steps we *need* (CONFIGURE / BUILD / INSTALL / TEST
    # / TEST_BEFORE_INSTALL) — those are set explicitly by superbuild and run
    # through the `build/exec` launcher, which our PE wrapper already fixes.
    "DOWNLOAD_COMMAND",
    "UPDATE_COMMAND",
    "PATCH_COMMAND",
    "CONFIGURE_COMMAND",   # keep but will only override *empty* ones → no-op
    "BUILD_COMMAND",       # ditto
    "INSTALL_COMMAND",     # ditto
    "TEST_COMMAND",
    "TEST_BEFORE_INSTALL_COMMAND",
    # Non-standard step names sometimes used by shinchiro.  Force them to
    # empty by keyword match later — we don't need them when using
    # SINGLE_SOURCE_LOCATION.
)


def _is_explicitly_set_elsewhere(step_cmd: str, ep_body: str) -> bool:
    """Return True if a command like `DOWNLOAD_COMMAND` is already set in the
    ExternalProject body (even if it's empty string) — so we don't double-set.

    Detection: whole-word match of step_cmd, not inside a comment, followed
    by whitespace/quote/newline (we don't inspect the value here).
    """
    return bool(re.search(rf"(?m)^\s*{re.escape(step_cmd)}\b", ep_body))


def force_ep_noop_steps(packages_dir: pathlib.Path) -> int:
    """Find every `ExternalProject_Add(` block in every `*.cmake` under
    packages_dir/ — inside each block, for every step in EP_STEP_COMMANDS_TO_NOOP
    that is NOT already set explicitly, append a `STEP_COMMAND ""` line right
    before the closing `)`.  By overwriting the step defaults this way, the
    generated `<step>--impl.cmake` file never contains a shell-only execute_process.

    Returns number of files changed.
    """
    STEPS = (
        "DOWNLOAD_COMMAND",
        "UPDATE_COMMAND",
        "PATCH_COMMAND",
        "TEST_COMMAND",
        "TEST_BEFORE_INSTALL_COMMAND",
    )
    changed = 0
    for f in sorted(packages_dir.glob("*.cmake")):
        text = read(f)
        if "ExternalProject_Add" not in text:
            continue
        original = text

        def _patch_ep(match):
            head = match.group(1)          # line `ExternalProject_Add(name\n` or `(name args\n`
            body = match.group(2)          # everything up to (but not including) closing ')'
            tail_paren = match.group(3)    # the closing paren itself + trailing newline
            added_lines = []
            indent = "    "
            # Derive indent from the first non-empty body line if possible
            for line in body.splitlines():
                stripped = line.lstrip()
                if stripped:
                    indent = line[:len(line) - len(stripped)]
                    break
            for step in STEPS:
                if _is_explicitly_set_elsewhere(step, body):
                    # Already explicit (even `""`) → don't override.  But if the
                    # caller wrote UPDATE_COMMAND with no value after it (not
                    # empty string, just missing), we still want it handled by
                    # regex above.  Explicit empty strings `""` are respected.
                    continue
                added_lines.append(f"{indent}{step} \"\"")
            if not added_lines:
                return head + body + tail_paren
            injection = "\n".join(added_lines) + "\n"
            # Insert right before closing ')'
            return head + body.rstrip("\n") + "\n" + injection + "\n" + tail_paren

        # Match ExternalProject_Add(...name\n...\n)  —  balanced parens inside
        # are rare in these EP files, so a non-greedy `(?s).*?` stopping at a
        # ^\s*\) line is accurate enough for the upstream format.
        #
        # Pattern:  ExternalProject_Add ( <ws>* <name> \n
        #           (then anything until a line that is just whitespace + ')'
        #            or a line that ends with ')' and that ')' is the only
        #            non-whitespace char on it)
        text2 = re.sub(
            r"(?ms)(^\s*ExternalProject_Add\s*\([^\n]*\n)"
            r"(.*?)"
            r"(^\s*\)\s*\n)",
            _patch_ep,
            text,
        )
        if text2 != original:
            write(f, text2)
            print(f"   force-ep-noop: {f.as_posix()}")
            changed += 1
    return changed


FF_MARK  = "# --- CI_FF_DISABLE_OPENSSL_INJECTED_v122 ---"
MPV_MARK = "# --- CI_MPV_SCHANNEL_INJECTED_v122 ---"

FF_SNIPPET = textwrap.dedent(r"""
    # --- CI_FF_DISABLE_OPENSSL_INJECTED_v122 ---
    if(COMMAND list)
      foreach(_v IN ITEMS
          FFMPEG_CONFIGURE_ARGS              ffmpeg_CONFIGURE_ARGS
          FFMPEG_OPTIONS                     ffmpeg_OPTIONS
          FFMPEG_EXTRA_OPTIONS               ffmpeg_EXTRA_OPTIONS
          FFMPEG_ADDITIONAL_CONFIGURE_FLAGS  ffmpeg_ADDITIONAL_CONFIGURE_FLAGS
          FFMPEG_CONFIGURE_EXTRA_FLAGS       ffmpeg_CONFIGURE_EXTRA_FLAGS
          FFMPEG_EXTRA_CONFIGURE_ARGS        ffmpeg_EXTRA_CONFIGURE_ARGS
          FFMPEG_EXTRA                       ffmpeg_EXTRA)
        if(DEFINED ${_v})
          list(APPEND ${_v}
            "--disable-openssl"
            "--enable-schannel"
            "--disable-gnutls"
            "--disable-mbedtls"
            "--disable-libtls")
          set(${_v} "${${_v}}" CACHE INTERNAL "" FORCE)
        endif()
      endforeach()
    endif()
""")

MPV_SNIPPET = textwrap.dedent(r"""
    # --- CI_MPV_SCHANNEL_INJECTED_v122 ---
    if(COMMAND list)
      foreach(_v IN ITEMS
          MPV_MESON_OPTIONS               mpv_MESON_OPTIONS
          MPV_OPTIONS                     mpv_OPTIONS
          MPV_EXTRA_OPTIONS               mpv_EXTRA_OPTIONS
          MPV_ADDITIONAL_MESON_FLAGS      mpv_ADDITIONAL_MESON_FLAGS
          MPV_CONFIGURE_ARGS              mpv_CONFIGURE_ARGS
          MPV_EXTRA_MESON_OPTIONS         mpv_EXTRA_MESON_OPTIONS
          MPV_MESON_EXTRA_FLAGS           mpv_MESON_EXTRA_FLAGS
          MPV_MESON_ARGS                  mpv_MESON_ARGS)
        if(DEFINED ${_v})
          list(APPEND ${_v} "-Dopenssl=disabled" "-Dtls-backend=schannel")
          set(${_v} "${${_v}}" CACHE INTERNAL "" FORCE)
        endif()
      endforeach()
    endif()
""")


def inject_fallback_var_snippets() -> None:
    """Append FF/MPV cache-var APPEND snippets to every relevant file."""
    pattern_paths = (
        glob.glob("packages/*.cmake")
        + glob.glob("cmake/*.cmake")
        + glob.glob("**/CMakeLists.txt", recursive=True)
    )
    seen = set()
    for rel in sorted(pattern_paths):
        p = pathlib.Path(rel)
        if not p.is_file():
            continue
        key = p.resolve()
        if key in seen:
            continue
        seen.add(key)
        try:
            content = read(p)
        except Exception:
            continue
        low = content.lower()
        name_low = p.name.lower()
        changed = False

        hit_ff = ("ffmpeg" in low) or ("ffmpeg" in name_low)
        hit_mpv = ("mpv" in low) or ("mpv" in name_low)

        if hit_ff and FF_MARK not in content:
            content += FF_SNIPPET
            changed = True
        if hit_mpv and MPV_MARK not in content:
            content += MPV_SNIPPET
            changed = True
        if changed:
            write(p, content)
            print(f"   fallback-inject: {p.as_posix()}")


def dump_tail(title: str, text: str, n: int = 40) -> None:
    banner(title)
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        print(f"{i:>4}  {line}")
        if i >= n and n > 0:
            break


def main() -> int:
    if len(sys.argv) < 5:
        print("usage: patch_superbuild.py SB PARENT_WIN MPV_RELEASE_DIR MPV_MPV_DIR",
              file=sys.stderr)
        return 2
    SB, PARENT_WIN, REL_DIR, MPV_DIR = sys.argv[1:5]
    banner("patch_superbuild.py args")
    print(f"SB          = {SB}")
    print(f"PARENT_WIN  = {PARENT_WIN}")
    print(f"MPV_RELEASE = {REL_DIR}")
    print(f"MPV         = {MPV_DIR}")

    os.chdir(SB)
    assert pathlib.Path("packages").is_dir(), "expected packages/ subdir in SB"

    # ---- mpv-release.cmake ----
    pr = pathlib.Path("packages/mpv-release.cmake")
    if pr.is_file():
        dump_tail("packages/mpv-release.cmake  [PRE-PATCH]", read(pr))
        write(pr, patch_mpv_release(read(pr)))
        dump_tail("packages/mpv-release.cmake  [POST-PATCH]", read(pr))
    else:
        print(f"[WARN] {pr} does not exist", file=sys.stderr)

    # ---- mpv.cmake ----
    pm = pathlib.Path("packages/mpv.cmake")
    if pm.is_file():
        dump_tail("packages/mpv.cmake  [PRE-PATCH] (head 60 lines)", read(pm), n=60)
        write(pm, patch_mpv(read(pm)))
        dump_tail("packages/mpv.cmake  [POST-PATCH] (head 70 lines)", read(pm), n=70)
    else:
        print(f"[WARN] {pm} does not exist", file=sys.stderr)

    # ---- ffmpeg.cmake ----
    pf = pathlib.Path("packages/ffmpeg.cmake")
    if pf.is_file():
        t = read(pf)
        dump_tail("packages/ffmpeg.cmake  [PRE-PATCH] (head 60 + tail 40)", t, n=100)
        write(pf, patch_ffmpeg(t))
        tail = "\n".join(read(pf).splitlines()[-60:])
        dump_tail("packages/ffmpeg.cmake  [POST-PATCH] (last 60 lines)", tail, n=0)
    else:
        print(f"[WARN] {pf} does not exist", file=sys.stderr)

    # ---- fallbacks (append to every potentially relevant cmake file) ----
    banner("fallback cache-variable APPEND injections")
    inject_fallback_var_snippets()

    # ---- summary: git diff stat ----
    banner("git diff --stat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
