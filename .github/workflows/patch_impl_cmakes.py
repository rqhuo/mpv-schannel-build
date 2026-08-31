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


def patch_execute_process(text: str) -> str:
    """Return text with every execute_process(COMMAND …) call that looks unsafe
    rewritten through bash -lc."""
    out = []
    i = 0
    n = len(text)
    # We search case-insensitively (CMake is case-insensitive for keywords) but
    # all upstream impl.cmake use lowercase execute_process.
    pattern = re.compile(r"execute_process\s*\(", re.IGNORECASE)
    while i < n:
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
        # Now we're INSIDE execute_process(...). Parse lineary to find all
        # COMMAND <tokens> <stop_word_or_next_COMMAND_or_paren_end>.
        j = m.end()
        paren_depth = 1
        # We'll build a new body of execute_process args, rewriting COMMAND
        # clauses as we go.
        depth_delta_on_close = 0
        last_was_command_keyword = False
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
                # Collect argv tokens until stop word or boundary. Use the
                # same tokenizer loop but do NOT write anything yet — we
                # decide first whether to rewrite.
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
                # Now `scan_pos` is positioned at the char after the argv
                # tokens end (or first stop-word char). We need to advance
                # text-scan `j` past all chars of argv tokens so the main
                # outer loop doesn't double-process them.
                j = scan_pos
                if argv and not first_token_is_safe_pe(argv[0]):
                    # Wrap this COMMAND argv through bash -lc
                    shell_str = bash_recompose(argv)
                    # Produce:   bash -lc "<shell>"
                    # bash -lc uses the *next* arg as $0 if given args after
                    # the command string, which is fine — we don't need args
                    # here because we already re-quoted them inline.
                    out.append(" bash -lc ")
                    # CMake-style quote the full shell string.
                    escaped = shell_str.replace("\\", "\\\\").replace('"', '\\"')
                    out.append(f'"{escaped}"')
                    # (j already moved to scan_pos, skip old argv below)
                    continue
                else:
                    # argv[0] safe: emit the original argv tokens verbatim so
                    # we don't risk any quoting regression. We don't need to
                    # reconstruct them from `argv` because they're still in
                    # `text` between original `j_before_argv` and scan_pos.
                    # But we lost that j_before_argv because we used j
                    # directly. So reconstruct: copy from earliest j_before
                    # which is ... we don't have it. Easier: write argv out.
                    out.append(" " + " ".join(argv))
                    continue
            # Any non-COMMAND, non-() keyword — copy verbatim
            out.append(tok)
            j = k
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
    for p in sorted(build_dir.rglob("*--impl.cmake")):
        if not p.is_file():
            continue
        count_seen += 1
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[WARN] cannot read {p}: {e}", file=sys.stderr)
            continue
        if "execute_process" not in src:
            continue
        dst = patch_execute_process(src)
        if dst != src:
            try:
                p.write_text(dst, encoding="utf-8")
            except OSError as e:
                print(f"[WARN] cannot write {p}: {e}", file=sys.stderr)
                continue
            count_patched += 1
            print(f"   patched: {p.relative_to(build_dir).as_posix()}")
    print(f"\nDONE patch_impl_cmakes.py: scanned {count_seen} *--impl.cmake files, "
          f"patched COMMAND in {count_patched} of them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
