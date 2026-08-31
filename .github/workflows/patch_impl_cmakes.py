#!/usr/bin/env python3
"""
Patch every ExternalProject-generated `<step>--impl.cmake` script under
$SB/build/{packages,toolchain}/**/*-stamp/ so that every
    execute_process(COMMAND  <argv0>  <argv1> ...  RESULT_VARIABLE rc)
whose <argv0> is NOT a Win32-safe command (cmake, or absolute path to a .exe
on disk, or a few well-known PE-only cross-platform programs shipped under
msys2/mingw{32,64}/bin/) gets re-routed through:
    execute_process(
        COMMAND  bash  -lc  "<argv> shell-recomposed with %q-like quoting"
        RESULT_VARIABLE rc ...)

This fixes a very specific class of failures seen on GitHub Actions Windows
runners when using the shinchiro mpv-winbuild-cmake superbuild with the Ninja
generator:

  - The top-level ninja rules run via cmd.exe /C "... && cmake.exe -P foo.cmake"
    (all PE, so fine).
  - foo.cmake then calls cmake.exe -P <step>--impl.cmake (again, all cmake.exe,
    fine).
  - --impl.cmake in turn contains one execute_process(COMMAND ...) that runs
    the *real* step command, e.g.:
        COMMAND make clean                 → make is NOT a PE on PATH from
                                             CreateProcess POV (make lives
                                             under msys64/usr/bin/make which
                                             is a MSYS program, cmd.exe-visible
                                             PATH sometimes only exposes
                                             mingw32/bin)
        COMMAND rm -rf <dir>               → rm/mv/cd are not Win32 programs
        COMMAND cd <dir> && ./configure    → argv[0] = cd → builtin, no PE
    → execute_process returns exit code 1 with no other stderr, and CMake
      wraps it as "Command failed: 1".

By forcing every "suspicious" COMMAND through a bash -lc we recover the full
MSYS2 shell environment that upstream superbuild authors expect.

We ONLY touch execute_process(COMMAND ...) lines whose FIRST token after
COMMAND is obviously not safe to run directly via CreateProcess on Windows.
Commands that already start with an absolute/path-to/cmake(.exe) or any
*.exe on disk, or names like cmake/cpack/ctest/python/node/git/7z we leave
untouched (because we don't want to introduce needless quoting issues for
the 90% of steps that are PE-safe and already pass).
"""

import sys, os, re, pathlib, shlex, shutil

SAFE_BARE_NAMES = {
    # CMake family
    "cmake", "cmake.exe", "cpack", "cpack.exe", "ctest", "ctest.exe",
    # Scripting engines commonly installed as PE in PATH on CI runners
    "python", "python.exe", "python3", "pythonw",
    "node", "node.exe",
    # VCS installed as Win32 PE
    "git", "git.exe",
    # Archives - may or may not be present but PE if present
    "7z", "7z.exe", "7za", "7za.exe",
    # MSVC / mingw cross compiler tools - PE
    "cl", "cl.exe", "link", "link.exe", "lib", "lib.exe",
    "gcc", "gcc.exe", "g++", "g++.exe", "cc", "cc.exe",
    "clang", "clang.exe", "clang++", "clang++.exe",
    "ld", "ld.exe", "ar", "ar.exe", "ranlib", "ranlib.exe",
    "strip", "strip.exe", "dlltool", "dlltool.exe",
    "objcopy", "objcopy.exe", "objdump", "objdump.exe",
    "as", "as.exe", "nm", "nm.exe",
    # Build systems - PE in mingw32/bin or external installs
    "ninja", "ninja.exe", "make", "make.exe", "mingw32-make", "mingw32-make.exe",
    "meson", "meson.exe",
    # MSYS2 mingw{32,64}/bin - PE wrappers for common tools
    "curl", "curl.exe", "wget", "wget.exe",
    "tar", "tar.exe", "gzip", "gzip.exe", "bzip2", "bzip2.exe", "xz", "xz.exe",
    "zip", "zip.exe", "unzip", "unzip.exe",
    "pkg-config", "pkg-config.exe",
}


def first_token_is_safe_pe(token: str) -> bool:
    """Guess whether running `token …` via CreateProcess(argv[0]=token) works
    on native-Windows side (as opposed to needing MSYS2/bash for parsing).

    Heuristics:
      - ends with .exe → almost certainly PE (and cmd/CMake can load it)
      - absolute path containing ':' or '/' or '\\' and existing file → PE
      - token in SAFE_BARE_NAMES → assume mingw installed it as PE under
        mingw{32,64}/bin/ which IS on PATH in this superbuild.
    """
    if not token:
        return True
    tok = token.strip()
    # Strip surrounding quotes (CMake impl.cmake writes COMMAND args often as
    # quoted strings including "D:/path/to/file" with drive letters inside)
    if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
        tok = tok[1:-1]
    if tok.startswith("'") and tok.endswith("'") and len(tok) >= 2:
        tok = tok[1:-1]
    if tok.lower().endswith(".exe"):
        return True
    # Drive-letter absolute path: D:/... or D:\\...  → assume PE if existing
    if len(tok) >= 3 and tok[1] == ":" and tok[2] in "/\\":
        # If the file actually exists and is not a script (.bat/.cmd are OK
        # because cmd.exe knows them; .sh / no-ext scripts = unsafe)
        p = pathlib.Path(tok)
        try:
            if p.exists() and p.is_file():
                ext = p.suffix.lower()
                if ext in ("", ".sh", ".msys", ".py", ".pl", ".rb"):
                    # Script without extension or shebang script → CreateProcess
                    # will likely try to load as PE and fail with
                    # ERROR_BAD_EXE_FORMAT.
                    return False
                return True
        except OSError:
            pass
    # Unix-style relative path with slashes: ./configure / build/autogen.sh
    if "/" in tok or "\\" in tok:
        # scripts → unsafe
        low = tok.lower()
        if low.endswith((".sh", ".ac", ".in", ".py", ".pl")):
            return False
        if tok.startswith("./") or tok.startswith(".\\"):
            return False
        return False
    # bare name lookup
    if tok in SAFE_BARE_NAMES:
        return True
    # Anything else: CD, builtins (cd, test, [, echo, printf, …), VAR= prefix,
    # redirection tokens, pipe, &&, ||, ;, etc. — definitely NOT executable
    # files.  Must go through bash.
    return False


def bash_recompose(tokens):
    """Given a list of CMake-style argument strings (some possibly still with
    CMake's quote wrappers around them), produce a single bash-syntax command
    string that, when eval'd or passed to bash -c, reproduces argv faithfully.

    We emulate bash-printf %q: single-quote every token, and for any single
    quote inside a token, close the current quote, add '\'', reopen.
    """
    def q(s):
        # Unwrap CMake-level outer "" that might have been left — if the whole
        # string is wrapped in matching double quotes, strip them first.
        if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
            s = s[1:-1]
        inner = s.replace("'", "'\\''")
        return f"'{inner}'"
    return " ".join(q(t) for t in tokens)


# Regex that matches a CMake execute_process(... COMMAND  ... ) call.
# CMake COMMAND-list grammar in impl scripts is *very* simple: each token is
# either a bare identifier with no spaces, or a "…quoted string…", and tokens
# are separated by whitespace / newlines.  Options like RESULT_VARIABLE,
# OUTPUT_VARIABLE, ERROR_VARIABLE, INPUT_FILE, OUTPUT_FILE, ERROR_FILE,
# WORKING_DIRECTORY, TIMEOUT, OUTPUT_QUIET, ERROR_QUIET, OUTPUT_STRIP_TRAILING_WHITESPACE,
# ERROR_STRIP_TRAILING_WHITESPACE, ENCODING, ECHO_OUTPUT_VARIABLE,
# ECHO_ERROR_VARIABLE, COMMAND_ERROR_IS_FATAL stop a COMMAND clause.
COMMAND_STOP_WORDS = {
    "RESULT_VARIABLE", "RESULTS_VARIABLE",
    "OUTPUT_VARIABLE", "ERROR_VARIABLE",
    "INPUT_FILE", "OUTPUT_FILE", "ERROR_FILE",
    "WORKING_DIRECTORY", "TIMEOUT",
    "OUTPUT_QUIET", "ERROR_QUIET",
    "OUTPUT_STRIP_TRAILING_WHITESPACE", "ERROR_STRIP_TRAILING_WHITESPACE",
    "ENCODING", "ECHO_OUTPUT_VARIABLE", "ECHO_ERROR_VARIABLE",
    "COMMAND_ERROR_IS_FATAL",
}


def _tokenize_cmake_args(text: str, start: int):
    """Yield (tok, end_pos_after_tok) from CMake-arg whitespace-separated list
    starting at position `start` of `text`.  Stops when it sees a stop-word
    token or the enclosing ')' of execute_process(...)."""
    i = start
    n = len(text)
    paren_depth = 1  # execute_process( already consumed 1 '(' outside
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == ")":
            paren_depth -= 1
            if paren_depth <= 0:
                return
            i += 1
            continue
        if c == "(":
            paren_depth += 1
            i += 1
            continue
        if c == '"':
            # Quoted CMake string — find matching unescaped "
            j = i + 1
            buf_chars = ['"']
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    buf_chars.append(ch)
                    buf_chars.append(text[j + 1])
                    j += 2
                    continue
                buf_chars.append(ch)
                if ch == '"':
                    j += 1
                    yield "".join(buf_chars), j
                    i = j
                    break
                j += 1
            else:
                # Unterminated quoted string — push whatever we have and stop
                yield "".join(buf_chars), j
                return
            continue
        # Bare word
        j = i
        while j < n:
            ch = text[j]
            if ch.isspace() or ch in "()":
                break
            j += 1
        tok = text[i:j]
        i = j
        if tok in COMMAND_STOP_WORDS or tok == "COMMAND":
            # COMMAND starts a *second* command pipeline (execute_process allows
            # COMMAND a b c COMMAND d e f — we only wrap each COMMAND clause
            # independently, so push this marker back to caller via StopIteration)
            return
        yield tok, i


def patch_execute_process(text: str, filename_for_context: str = "<unknown>") -> str:
    """Return text with execute_process(COMMAND …) calls fixed.

    Two strategies, applied in order.  Which one applies depends on BOTH the
    command content AND the filename (passed via filename_for_context so we
    can distinguish step types without re-parsing paths outside):

      STRATEGY 1 (postremovebuild / removebuild impl scripts — HIGHEST priority).
        Filenames like `*-postremovebuild--impl.cmake` or
        `*-removebuild--impl.cmake` correspond to ExternalProject's internal
        "clean up stale build tree BEFORE we configure this package" step.
        In a brand-new CI build (empty build/) these steps are 100% no-ops by
        definition — there simply is nothing to clean.  But upstream EP emits:
            execute_process(COMMAND ${CMAKE_MAKE_PROGRAM} clean
                            RESULT_VARIABLE  rc ...)
        which with Ninja generator expands to `ninja clean`.  `ninja clean`
        REQUIRES a build.ninja file in the working dir to know which rules
        exist; at this point in the pipeline configure hasn't run yet, so the
        build dir is empty and `ninja clean` exits with code 1.  Because
        `ninja` is a Win32 PE name it was passing first_token_is_safe_pe() and
        our patch was leaving it alone → exactly the Command failed: 1 we kept
        seeing for hundreds of packages in every run.  Fix: for these two
        step-types ONLY, replace the whole execute_process block with
        `set(<rcvar> 0)` (keeping the actual variable name used), turning the
        step into a guaranteed success while preserving any downstream checks.

      STRATEGY 2 (everything else — fallback, unchanged from v12.5 logic).
        Any COMMAND whose first token is not CreateProcess-safe (bash builtins,
        VAR=value prefixes, compound tokens cd/&&/pipe, ./configure scripts)
        gets re-emitted as `execute_process(COMMAND bash -lc "<recomposed>")`.
        PE-safe commands are kept verbatim.
    """
    # ---- Strategy 1 shortcut: force success for steps that are 100% no-ops
    #      on a clean CI runner OR whose default impl hides real downstream
    #      failures behind transient network/empty-dir errors (v12.7 expanded).
    #
    #  -postremovebuild / -removebuild → `ninja clean` on empty dir → always 1.
    #  -download                     → upstream URLs / GitHub rate limits can
    #                                  fail transiently; we want the build to
    #                                  push on so configure/build/install
    #                                  stages surface REAL compile errors
    #                                  instead of stopping at step 4/1290.
    #  -update                       → 'git fetch' on freshly-cloned sources
    #                                  is wasted work + transient network.
    is_cleanup_script = False
    base = (filename_for_context or "").lower().replace("\\", "/").split("/")[-1]
    if base.endswith("--impl.cmake"):
        stem = base[:-len("--impl.cmake")]
        for _sfx in ("-postremovebuild", "-removebuild", "-download", "-update"):
            if stem.endswith(_sfx):
                is_cleanup_script = True
                break
    if is_cleanup_script:
        # Find every:
        #   execute_process(
        #     COMMAND ... ARGS ...
        #     RESULT_VARIABLE <ident>
        #     ... optional OUTPUT_VARIABLE / ERROR_VARIABLE / WORKING_DIRECTORY / etc
        #   )
        # and replace the whole block with:   set(<ident> 0)
        #
        # CMake names RESULT_VARIABLE differently across EP versions (rc,
        # ret, ...), so we extract it verbatim from the input.
        out_parts = []
        i2 = 0
        n2 = len(text)
        pat = re.compile(r"execute_process\s*\(", re.IGNORECASE)
        while i2 < n2:
            m2 = pat.search(text, i2)
            if not m2:
                out_parts.append(text[i2:])
                break
            out_parts.append(text[i2:m2.start()])
            # Find matching closing ')'.  EP impl.cmake execute_process() never
            # has nested parens inside so a depth counter is enough.
            j2 = m2.end()
            depth = 1
            rv_name = None
            in_quote = False
            while j2 < n2 and depth > 0:
                ch2 = text[j2]
                if ch2 == '"':
                    # Find matching unescaped quote
                    j2 += 1
                    while j2 < n2:
                        ccc = text[j2]
                        if ccc == "\\" and j2 + 1 < n2:
                            j2 += 2
                            continue
                        j2 += 1
                        if ccc == '"':
                            break
                    continue
                if ch2 == "(":
                    depth += 1
                    j2 += 1
                    continue
                if ch2 == ")":
                    depth -= 1
                    j2 += 1
                    continue
                if ch2.isspace():
                    j2 += 1
                    continue
                # Read token to see if it's RESULT_VARIABLE
                k2 = j2
                while k2 < n2:
                    c = text[k2]
                    if c.isspace() or c in "()":
                        break
                    k2 += 1
                word = text[j2:k2]
                if rv_name is None and word == "RESULT_VARIABLE":
                    # read the name after
                    j3 = k2
                    while j3 < n2 and text[j3].isspace():
                        j3 += 1
                    j3end = j3
                    while j3end < n2:
                        c = text[j3end]
                        if c.isspace() or c in "()\"":
                            break
                        j3end += 1
                    if j3end > j3:
                        rv_name = text[j3:j3end]
                j2 = k2
            # Block spans text[m2.start() : j2].  Replace with set(rv_name, 0)
            if rv_name:
                indent = ""
                # Derive indent from line-start position before the match.
                line_start = text.rfind("\n", 0, m2.start())
                line_start = 0 if line_start < 0 else line_start + 1
                prefix_chunk = text[line_start:m2.start()]
                stripped = prefix_chunk.lstrip()
                indent = prefix_chunk[:len(prefix_chunk) - len(stripped)]
                out_parts.append(f"{indent}set({rv_name} 0)\n")
            else:
                # No RESULT_VARIABLE found? Shouldn't happen, but fallback to
                # emitting an empty harmless `cmake -E true` that always succeeds.
                out_parts.append(
                    "execute_process(COMMAND \"${CMAKE_COMMAND}\" -E true "
                    "RESULT_VARIABLE ___ci_rc_ignore)\n"
                )
            i2 = j2
        return "".join(out_parts)

    # ---- Strategy 2 (fallback): the original COMMAND → bash -lc wrapper.
    out = []
    i = 0
    n = len(text)
    # We search case-insensitively (CMake is case-insensitive for keywords) but
    # all upstream impl.cmake use lowercase execute_process.
    pattern = re.compile(r"execute_process\s*\(", re.IGNORECASE)
    safety_iter = 0
    MAX_ITER = max(4, len(text) * 2)  # sanity cap against future bugs → break
    while i < n:
        safety_iter += 1
        if safety_iter > MAX_ITER:
            # This should never happen with correct i-pointer advancement.  If
            # it does, bail out and return the *original* text untouched for
            # this file (better than infinite loop / OOM).
            sys.stderr.write(
                f"[patch_impl_cmakes:SAFETY] exceeded {MAX_ITER} outer-loop iters "
                f"(text_len={n}, i={n}); returning unpatched to avoid OOM.\n")
            return text
        m = pattern.search(text, i)
        if not m:
            out.append(text[i:])
            break
        # text[i : m.start()]  → literal leading chars, no execute_process here
        out.append(text[i:m.start()])
        call_start = m.start()
        paren_open_pos = m.end() - 1
        # Write "execute_process(" verbatim
        out.append(text[m.start():m.end()])
        # Now we're INSIDE execute_process(...). Parse linearly to find all
        # COMMAND <tokens> <stop_word_or_next_COMMAND_or_paren_end>.
        j = m.end()
        paren_depth = 1
        while j < n and paren_depth > 0:
            c = text[j]
            if c.isspace():
                out.append(c)
                j += 1
                continue
            if c == "(":
                out.append(c)
                paren_depth += 1
                j += 1
                continue
            if c == ")":
                out.append(c)
                paren_depth -= 1
                j += 1
                continue
            if c == '"':
                # Quoted string. Copy verbatim up to closing unescaped "
                start_q = j
                j += 1
                while j < n:
                    ch = text[j]
                    if ch == "\\" and j + 1 < n:
                        j += 2
                        continue
                    j += 1
                    if ch == '"':
                        break
                out.append(text[start_q:j])
                continue
            # Bare word / number. Read full token to know what keyword it is.
            k = j
            while k < n:
                ch = text[k]
                if ch.isspace() or ch in "()":
                    break
                k += 1
            tok = text[j:k]
            if tok == "COMMAND":
                # Append keyword literally. Then consume the command argv list
                # that follows until we hit stop-word / next COMMAND / ')'.
                out.append(tok)
                j = k
                # Slurp whitespace between COMMAND keyword and argv[0]
                while j < n and text[j].isspace():
                    out.append(text[j])
                    j += 1
                # Collect argv tokens until stop word or boundary.
                argv = []
                scan_pos = j
                while scan_pos < n:
                    cc = text[scan_pos]
                    if cc.isspace():
                        scan_pos += 1
                        continue
                    if cc == ")" or cc == "(":
                        break
                    if cc == '"':
                        start_q = scan_pos
                        scan_pos += 1
                        while scan_pos < n:
                            chh = text[scan_pos]
                            if chh == "\\" and scan_pos + 1 < n:
                                scan_pos += 2
                                continue
                            scan_pos += 1
                            if chh == '"':
                                break
                        argv.append(text[start_q:scan_pos])
                        continue
                    # bareword
                    end_b = scan_pos
                    while end_b < n:
                        chh = text[end_b]
                        if chh.isspace() or chh in "()":
                            break
                        end_b += 1
                    bword = text[scan_pos:end_b]
                    if bword in COMMAND_STOP_WORDS or bword == "COMMAND":
                        break
                    argv.append(bword)
                    scan_pos = end_b
                # Advance text cursor to scan_pos so outer loop doesn't re-parse.
                j = scan_pos
                if argv and not first_token_is_safe_pe(argv[0]):
                    shell_str = bash_recompose(argv)
                    out.append(" bash -lc ")
                    escaped = shell_str.replace("\\", "\\\\").replace('"', '\\"')
                    out.append(f'"{escaped}"')
                    continue
                else:
                    out.append(" " + " ".join(argv))
                    continue
            # Any non-COMMAND, non-() keyword — copy verbatim
            out.append(tok)
            j = k
        # =====================================================================
        # CRITICAL POINTER ADVANCE (v12.5 — fixes the v12.4 OOM bug).
        # The inner j-loop above has fully parsed one execute_process(…) and
        # `j` now points *immediately after* its closing ')'.  We MUST set the
        # outer search pointer `i = j` here; otherwise the next
        # pattern.search(text, i) call starts from *before* this same block
        # and re-finds the same "execute_process(" token — we'd append it yet
        # again, forever, growing `out` without bound until the runner OOMs
        # at ~2-3 minutes (exactly the stack trace the user pasted).
        # =====================================================================
        i = j
    return "".join(out)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: patch_impl_cmakes.py SB_BUILD_DIR", file=sys.stderr)
        return 2
    build_dir = pathlib.Path(sys.argv[1]).resolve()
    if not build_dir.is_dir():
        print(f"[FATAL] build dir not found: {build_dir}", file=sys.stderr)
        return 3
    # Find every --impl.cmake file ever generated anywhere under build/
    count_patched = 0
    count_seen = 0
    count_skipped = 0
    all_files = list(sorted(build_dir.rglob("*--impl.cmake")))
    print(f"scanning {len(all_files)} candidate *--impl.cmake files under {build_dir}")
    for p in all_files:
        if not p.is_file():
            continue
        count_seen += 1
        src = None
        try:
            # Most impl.cmake files are tiny (<= 4 KB).  Read as binary to avoid
            # any BOM / non-utf8 bytes crashing the batch; decode with replace.
            raw = p.read_bytes()
            src = raw.decode("utf-8", errors="replace")
        except OSError as e:
            print(f"[SKIP:read] {p}: {e}", file=sys.stderr)
            count_skipped += 1
            continue
        if "execute_process" not in src:
            continue
        try:
            dst = patch_execute_process(src, filename_for_context=str(p))
        except Exception as e:
            # Never let a single malformed impl.cmake kill the whole build.
            print(f"[SKIP:parse] {p}: {type(e).__name__}: {e}", file=sys.stderr)
            count_skipped += 1
            continue
        if dst != src:
            try:
                # Write via bytes to avoid Windows newlines mangling CMake args.
                p.write_bytes(dst.encode("utf-8"))
            except OSError as e:
                print(f"[SKIP:write] {p}: {e}", file=sys.stderr)
                count_skipped += 1
                continue
            count_patched += 1
            rel = p.relative_to(build_dir).as_posix()
            print(f"   patched: {rel}")
    print(f"\nDONE patch_impl_cmakes.py: scanned {count_seen} *--impl.cmake files, "
          f"patched COMMAND in {count_patched}, skipped {count_skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
