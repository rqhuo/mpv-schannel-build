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

# =========================================================================
# V22/V23 global: resolve bash.exe absolute path ONCE at import time.
#
# The CMake execute_process(COMMAND <argv-list>) calls that we rewrite in
# Strategy 0 are executed by the mingw cmake.exe (native Win32 PE process)
# inside a `cmd.exe /C "… cmake -P foo.cmake …"` subprocess.  The PE
# process's PATH block only knows native Windows paths, not POSIX-style
# `D:/…/usr/bin` mixed paths.  Even when MSYS2 bash sets POSIX PATH, the
# PE child inherits a partially-converted env block where `/usr/bin/bash`
# may or may not resolve.  Result: `execute_process(COMMAND bash …)` fails
# with 127 (command not found) even when the user's interactive shell says
# `which bash` OK.
#
# So instead of emitting bare `"bash"` we emit the ABSOLUTE Windows path to
# bash.exe (backslashes are fine, forward slashes also work because our
# CMake callers are mingw cmake on NT).  Only if we cannot resolve it at
# script-import time do we fall back to the bare name + hope PATH helps.
# =========================================================================
def _resolve_bash_exe() -> str:
    # 1) Prefer FORWARD-SLASH candidates on disk.  CreateProcess on NT fully
    #    accepts "/" as a path separator inside absolute paths, AND they need
    #    NO CMake escape treatment when embedded in set(command "…") strings
    #    (the only backslash we'd otherwise have to double up is gone).
    #    GitHub Actions default runner paths first, then local msys64.
    candidates_fwd = [
        r"D:/a/_temp/msys64/usr/bin/bash.exe",
        r"C:/msys64/usr/bin/bash.exe",
    ]
    for p in candidates_fwd:
        if os.path.isfile(p):
            return p
    # 2) Try the backslash variants as a fall-back.  If we land here, the
    #    caller MUST run _cmake_quote_for_set(token) on it before putting
    #    the value into a CMake double-quoted string, or CMake will choke on
    #    Invalid character escape '\a' / '\t' / '\u' etc.
    candidates_bs = [
        r"D:\a\_temp\msys64\usr\bin\bash.exe",
        r"C:\msys64\usr\bin\bash.exe",
    ]
    for p in candidates_bs:
        if os.path.isfile(p):
            return p
    # 3) Use shutil.which (respects Python's inherited PATH).
    #    `bash.exe` is the PE binary name; "bash" as a fallback basename.
    p = shutil.which("bash.exe") or shutil.which("bash")
    if p:
        return p
    # 4) Last-resort bare name (rely on PATH at execute_process time, and
    #    on the caller _cmake_quote_for_set()'ing it — it's a bare word so
    #    no escaping is actually needed).
    return "bash"

BASH_EXE_ABS = _resolve_bash_exe()


# =========================================================================
# V20 Strategy 0 helpers — fix build/exec launcher AT THE set(command "...")
# level so that Strategy 3 (${command} placeholder guard) no longer skips
# the only calls that actually matter for ffmpeg / libass / fontconfig …
# =========================================================================

def _is_build_exec_token(tok: str) -> bool:
    """Return True if tok is the superbuild's build/exec PE wrapper path."""
    if not tok:
        return False
    t = tok.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    t_low = t.lower().replace("\\", "/").rstrip(".exe")
    return t_low.endswith("/build/exec")


def _cmake_quote_for_set(s: str) -> str:
    """Escape a shell-command string so it can safely live inside
    CMake's outer set(command "…") double-quoted value.

    In CMake double-quoted strings:  \\ → literal backslash,  \" → literal ".
    Dollar-sign expansions like ${FOO} would fire but our shell commands
    don't contain them (bash_recompose uses single-quoted argv tokens).
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _cmake_split_args(inner: str):
    """Split the inner of a CMake set(command "<inner>") value into its
    semicolon-separated argument list.

    The naive `inner.split(";")` is incorrect because CMake COMMAND values
    often carry tokens with EMBEDDED double-quote wrappers (e.g.
    `-DCMAKE_C_FLAGS="-O2 -g"`) where the enclosing "" are part of the single
    argument's value; any `;` or literal character *inside* those "" is part
    of the argument and must NOT split the list.

    Rules (mirroring CMake's quoted-string semantics in double-quoted args):
      - We walk character-by-character.  `in_q` tracks whether we are inside
        an un-escaped double-quote run.
      - `\\"` (a literal `\"` inside the outer set(command "…") string) means
        the cmake-level string contains ONE literal `"` character → toggle
        in_q.
      - `;` only breaks the current item when in_q == False.
      - `\\\\` inside outer "" → one literal `\\`.
    """
    items = []
    cur = []
    i = 0
    n = len(inner)
    in_q = False
    while i < n:
        ch = inner[i]
        if ch == '\\' and i + 1 < n and inner[i + 1] in ('\\', '"'):
            # CMake backslash escape inside double-quoted value.  Preserve as
            # literal 2-char sequence in the current arg so bash_recompose sees
            # the real raw text (it strips "" wrappers later anyway).
            cur.append(inner[i:i + 2])
            i += 2
            continue
        if ch == '"':
            # Toggle "inside quoted arg" state.  Keep the " char in the token
            # because bash_recompose / _is_build_exec_token both know how to
            # strip outer CMake "" wrappers already.
            in_q = not in_q
            cur.append('"')
            i += 1
            continue
        if ch == ';' and not in_q:
            items.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    items.append("".join(cur))
    return items


def _find_setcommand_boundary(line: str):
    """Locate the `set(command "…")` call inside one line.

    The previous version used a regex with `[^"]*` as the inner capture group,
    which completely fails for legitimate COMMAND values that contain literal
    double-quote characters as part of an argument (e.g.
    `-DCMAKE_C_FLAGS="-O2 -g"`) — the pattern stops consuming characters at
    the FIRST embedded `"` instead of the outer closing `"` that belongs to
    the `set(command "...")` syntax itself → Strategy 0 match count 0.

    New parser: walk the line, find `set(…command …)`, then inside the parens
    consume the CMake-escaped double-quoted payload that follows `command`.
    Returns (prefix, inner, suffix) character ranges OR None if this line
    doesn't contain a parsable `set(command "<inner>")` call.
    """
    # Step 1: find "set(" (case-insensitive) and its matching ')'.
    s = line
    # lenient lower-case scan for 'set('
    s_low = s.lower()
    set_start = s_low.find("set(")
    if set_start < 0:
        # Try whitespace variants: set  (  …  )
        m = re.search(r'\bset\s*\(', s, re.IGNORECASE)
        if not m:
            return None
        set_start = m.start()
        open_paren = m.end() - 1  # '('
    else:
        open_paren = set_start + 3
    # Step 2: find the `command` keyword followed immediately (with ws) by `"`.
    rest_i = open_paren + 1
    while rest_i < len(s) and s[rest_i].isspace():
        rest_i += 1
    # Must read exactly "command" (case-insensitive)
    if s_low[rest_i:rest_i + 7] != "command":
        return None
    rest_i += 7
    while rest_i < len(s) and s[rest_i].isspace():
        rest_i += 1
    if rest_i >= len(s) or s[rest_i] != '"':
        return None
    inner_open = rest_i  # outer opening " of set(command "…")
    # Step 3: locate the OUTER closing double-quote of the set(command "…")
    # syntax.
    #
    # The obvious forward-scan approach (pick the FIRST unescaped ") breaks
    # completely for stamp files whose arguments contain LITERAL double-quote
    # characters — superbuild likes to write values like
    # `-DCMAKE_C_FLAGS="-O2 -g"` where the inner quotes are part of the
    # argument, NOT CMake-level backslash-escapes.  Our original code would
    # therefore chop the value at the FIRST such embedded quote and produce:
    #   inner  = 'D:/…/svtav1-build;-G;Ninja;-DCMAKE_C_FLAGS='
    #   suffix = '"-O2 -g"")'
    # → all subsequent shell quoting is wrong.
    #
    # Instead: (a) first find the BALANCED closing parenthesis of set(…),
    # which is unambiguous because embedded parens would be inside "" and we
    # never nest additional cmake calls inside these stamp-file lines; then
    # (b) walk BACKWARDS from the ')' to find the LAST bare " before it —
    # THAT is the outer closing quote.
    paren_depth_start = 1  # set( opened one paren (already consumed)
    k = open_paren + 1
    close_paren = None
    while k < len(s):
        if s[k] == '(':
            paren_depth_start += 1
        elif s[k] == ')':
            paren_depth_start -= 1
            if paren_depth_start == 0:
                close_paren = k
                break
        k += 1
    if close_paren is None:
        return None
    # Walk backwards to find the LAST double-quote strictly before ')'.
    inner_close = None
    j = close_paren - 1
    while j > inner_open:
        if s[j] == '"':
            # Do NOT stop on a CMake-escaped \" — that is just a literal "
            # inside the value, not the outer string boundary.
            if j - 1 >= inner_open and s[j - 1] == '\\':
                # But \\" → literal \ followed by real quote — so only skip
                # when the preceding backslash count is ODD.
                bs_count = 0
                p = j - 1
                while p > inner_open and s[p] == '\\':
                    bs_count += 1
                    p -= 1
                if bs_count % 2 == 1:
                    j = p
                    continue
            inner_close = j
            break
        j -= 1
    if inner_close is None or inner_close <= inner_open:
        return None
    prefix = s[:inner_open]       # up to and NOT including the opening outer "
    inner = s[inner_open + 1:inner_close]  # between the outer ""
    suffix = s[inner_close:close_paren + 1]  # includes outer " + anything inside set() + ')'.
    return prefix, inner, suffix


def _patch_setcommand_buildexec(text: str) -> tuple:
    """Replace every line containing
         set(command "D:/…/build/exec;tok1;tok2;…")
    by
         set(command "bash;D:/…/build/exec.sh;tok1;tok2;…")

    Returns (new_text, replacement_count).

    V22 (THIS VERSION) — critical fix for "Command failed: 127":
    -----------------------------------------------------------------
    The original build/exec is a bash script (exec.sh after PE-wrapping)
    that sets critical environment variables BEFORE running the command:

        export PATH="/bin:…/.cargo/bin:$PATH"
        export PKG_CONFIG="pkgconf --static"
        export PKG_CONFIG_LIBDIR="/i686/lib/pkgconfig"
        export RUSTUP_HOME="…"
        export CARGO_HOME="…"
        eval $*

    V20/V21 replaced build/exec with `bash;-lc;<recomposed-shell-string>`,
    which bypassed exec.sh entirely → NO env setup → every configure step
    returned 127 (command not found) or 1 (missing PKG_CONFIG_PATH etc.).

    V22 fix: instead of recomposing into a single `bash -lc` string, simply
    replace the build/exec token with `bash;<exec_sh_path>` and KEEP the
    original semicolon-separated argument list intact.  This way CMake's
    `execute_process(COMMAND ${command})` calls:

        bash  D:/…/build/exec.sh  tok1  tok2  tok3  …

    which is EXACTLY what the original PE wrapper did (find bash.exe → run
    exec.sh with the same argv).  exec.sh sets up the environment and then
    `eval $*` runs the command.  No quoting/recomposition needed at all.
    """
    count = [0]
    lines = text.split("\n")
    new_lines = []
    for line in lines:
        bound = _find_setcommand_boundary(line)
        if bound is None:
            new_lines.append(line)
            continue
        prefix, inner, suffix = bound
        items = _cmake_split_args(inner)
        if not items or not _is_build_exec_token(items[0]):
            new_lines.append(line)
            continue
        # Derive exec.sh path: same as build/exec but with .sh appended.
        # _is_build_exec_token already stripped outer quotes, so items[0]
        # is the raw path.  We need to handle both quoted and unquoted cases.
        exec_path = items[0].strip()
        # Strip surrounding quotes if present
        if len(exec_path) >= 2 and exec_path[0] == '"' and exec_path[-1] == '"':
            exec_path = exec_path[1:-1]
        exec_sh_path = exec_path + ".sh"
        # ================================================================
        # V25: Use bash -c <kScript> <exec.sh_path> <user_tokens...> form,
        #   which is 100% semantically equivalent to exec_wrapper.c.
        # ================================================================
        #   Why not just `bash exec.sh user_tokens...`?
        #     Because that's "bash SCRIPT_MODE", where:
        #       $0 = exec.sh
        #       $1 = CONF=1  (if that was first user token)
        #     Then `eval $*` inside exec.sh expands positional params as
        #     CONF=1 D:/configure --host=... which *looks* right but:
        #       (a) the superbuild uses command-shapes like:
        #           cd <dir> && CONF=1 ./configure
        #           CONF=1 'cmake' '-H…'
        #           where `cd` / `&&` / CONF=1 were originally executed in a
        #           bash invocation via the PE wrapper's quoted shell script
        #           `_CMD=("$@"); set --; . "$0"; eval "$(printf ' %q' …)"`
        #           — this handles shell compound statements + env prefixes +
        #           shell builtins (argv[0]=cd which has no corresponding PE)
        #           — the simple SCRIPT_MODE `exec.sh eval $*` fails for them.
        #       (b) evidence: in logs_91151348466, Step 12 V24 DIAG:
        #             "$EXEC" echo "build/exec works" → OUT is empty but rc=0.
        #           The PE wrapper's `_spawnvp("bash", …)` → stdout capture
        #           via $() returned blank.  And S0-rewritten libiconv/brotli
        #           returned 127/1 from the same script-mode invocation.
        #   So we mirror exec_wrapper.c's exact argv layout:
        #     [0]  BASH_EXE_ABS                   absolute D:/…/bash.exe
        #     [1]  -c                             bash option
        #     [2]  kScript                        (same as exec_wrapper.c:
        #                                           _CMD=("$@"); set --; . "$0";
        #                                           eval "$(printf ' %q' …)")
        #     [3]  exec.sh_path                    → fills $0 in -c mode
        #     [4+] original user tokens            → fills $1, $2, …
        #   This means:
        #     _CMD=("$@")  → _CMD exactly = the original <user_token list>
        #     set --       → clear $1 $2 so `. exec.sh`'s trailing `eval $*`
        #                    is empty (as exec_wrapper.c always intended).
        #     . "$0"       → source exec.sh into the same shell (PATH/PKG_CONFIG
        #                    / RUSTUP_HOME etc. now apply for the eval below).
        #     eval "$(printf ' %q' "${_CMD[@]}")"
        #                  → every user token is round-tripped through bash
        #                    %q quoting → spaces/shell specials survive as a
        #                    single argv word; cd/&&/CONF=1 become shell syntax
        #                    again because eval re-parses the composed line.
        #
        #   This gives byte-for-byte identical runtime behaviour to the PE
        #   wrapper, but with BASH_EXE_ABS as the PE entrypoint (absolute
        #   path → guaranteed executable findability even if Windows-PATH
        #   PATH in the caller is mangled).
        K_SCRIPT = (
            '_CMD=("$@"); '
            'set --; '
            '. "$0"; '
            'eval "$(printf \' %q\' "${_CMD[@]}")"'
        )
        new_items = [BASH_EXE_ABS, "-c", K_SCRIPT, exec_sh_path] + items[1:]
        # SAFETY: Every token going back into the CMake set(command "…")
        # outer double-quote must have '\' and '"' escaped.  kScript contains
        # single-quotes + $ @ parens which are NOT CMake-escape sequences,
        # but the embedded " inside ${_CMD[@]} / "$0" / printf string all
        # need \" escaping via _cmake_quote_for_set.
        #
        # EXTRA SAFETY for the K_SCRIPT token: it contains literal ';'
        # characters between the four shell statement fragments:
        #   '_CMD=("$@"); set --; . "$0"; eval "$(printf …)"'
        # In CMake list values (";" is the separator inside set(command "…")'s
        # outer double-quote), a naked ';' would split it into multiple argv
        # entries → bash would see fragment 1 as the -c script and fragments
        # 2..4 become positional args, completely breaking the intended shell
        # program.  A previous attempt to wrap it with explicit \"...\" list
        # quotes failed because kScript *internally* contains `"$@"`, `"$0"`,
        # and `"$(printf …)"`, whose literal `"` chars close and reopen the
        # list-level quoting region, re-exposing the internal `;`s to
        # splitting.
        #
        # The correct fix: in CMake lists, `\;` is ALWAYS a literal semicolon
        # (it never triggers list splitting), regardless of whether list
        # parsing currently considers itself inside an element-level "…"
        # quote or not.  So we replace every `;` inside K_SCRIPT with `\;`
        # BEFORE running _cmake_quote_for_set (which doubles `\` and escapes
        # `"` inside the outer set(command "…") double-quote context).  The
        # whole per-token pipeline is:
        #
        #   K_SCRIPT:            foo; bar
        #   replace(";", "\\;"): foo\; bar
        #   _cmake_quote_for_set: foo\\\; bar        (\→\\, \;→\\;, "→\")
        #   → written into set(command "..."):  INNER contains  foo\\\; bar
        #
        #   CMake DQ parse: \\\;  →  literal  \;   (\\→\, \;→\;)  →  we have \;
        #   CMake list split:  \;  →  literal  ;   →  one argv:   "foo; bar"
        #
        # The embedded quotes `… \"…\"` inside the token likewise survive
        # through the same two steps as literal `"` characters on the list
        # string (the only place they could accidentally toggle a `\;` escape
        # would be immediately adjacent, and in our script they're not).
        quoted_items = []
        for tok in new_items:
            if tok is K_SCRIPT:
                #
                # K_SCRIPT must survive two independent CMake escape layers
                # and come out the far end as an argv byte-for-byte identical
                # to exec_wrapper.c's static const char kScript[]:
                #
                #   _CMD=("$@"); set --; . "$0"; eval "$(printf ' %q' "${_CMD[@]}")"
                #
                # The two CMake escape layers are:
                #
                #  [Layer 1] Outer set(command "...") CMake double-quote context
                #            (DQ parse):
                #            \\   → literal '\'
                #            \"   → literal '"'
                #            \X   → literal '\' + 'X' for unknown X (kept)
                #            Any other chars through unchanged.
                #
                #  [Layer 2] CMake list-value parsing of the *unescaped* string
                #            produced by Layer 1:
                #            "    → toggle in_q, produces NO output
                #            \X   → literal X (consumes 2 chars, emits 1)
                #            ;    → list separator  WHEN not in_q AND not \-escaped
                #            Any other char → literal.
                #
                # We need to produce the original kScript, containing both " and
                # ; characters — which each have special meaning in Layer 2.
                # We therefore pre-escape the raw K_SCRIPT token for Layer 2:
                #   A1)  ;  →  \;     (always literal semicolon in CMake lists)
                #   A2)  "  →  \"     (literal double-quote, not in_q toggle)
                # The $ @ % { } ( ) characters in kScript are inert for both
                # layers — CMake doesn't expand anything inside a plain
                # set(command "...") value unless ${var} is there, which our
                # script does not contain (all $ are inside literal bash strings).
                esc = tok
                esc = esc.replace(";", "\\;")    # Step 2: \; → literal ;
                esc = esc.replace('"', '\\"')   # Step 2: \" → literal " (no toggle)
                # Now apply the Layer 1 DQ escaping that _cmake_quote_for_set
                # provides:  '\' → '\\', '"' → '\"'
                esc = _cmake_quote_for_set(esc)
                #
                # Tracing the whole pipeline through cmake.exe:
                #   Raw char   After A1/A2   After _cmake_qfs   L1 parse → list   L2 parse → argv[2]
                #   ;          \;             \\;                \;                  ;
                #   "          \"             \\\"              \"                 "
                #   \ (none)   \-escaped was  N/A                 \                  \
                # So the final bash -c script arg equals the original static
                # const char kScript[] in exec_wrapper.c  — exact match.
            else:
                esc = _cmake_quote_for_set(tok)
            quoted_items.append(esc)
        new_inner = ";".join(quoted_items)
        new_line = f'{prefix}"{new_inner}"{suffix[1:]}'
        count[0] += 1
        new_lines.append(new_line)
    return "\n".join(new_lines), count[0]

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

    V21: the previous version wrapped EVERY non-SHELL_OP token in shell single
    quotes.  That caused three separate classes of failure inside the
    superbuild when paired with `set(command "bash;-lc;<recomposed>")` inside
    CMake stamp files, all visible in logs_91112829316:

      (A) `CONF=1 ./configure` appeared AFTER a shell control operator
          (`&&`) and the old leading-only scan let it fall through to
          default wrapping → `'CONF=1' './configure'` → bash tries to run a
          LITERAL command called "CONF=1" → exit 127.
      (B) Every plain argv word (`'cmake' '-G' 'Ninja' '-H<path>'`) was
          wrapped in single quotes.  Individually harmless, but when
          combined with (C) and CMake's double-quoted string-escape layer
          (via `_cmake_quote_for_set` → `\"` escapes) the final shell string
          ends up with quote-stacking that confuses the argument splitter.
      (C) Tokens carrying EMBEDDED CMake-level double quotes (e.g.
          `-DCMAKE_C_FLAGS="<space separated CFLAGS list>"`) had their
          outer "" preserved and the inner content escaped, causing shell
          single-quote layers to wrap the double-quote wrapper too, so
          CMake received the value as the literal concatenated string
          `"\"-foo=...\""` — i.e. with literal backslashes still present.

    New quoting policy (MINIMAL quotes):
      * SHELL_OPS        → emit verbatim, and re-enable env-prefix detection
                           so `cd … && CONF=1 ./configure` keeps CONF=1 as
                           an ENV prefix not an argv[0].
      * NAME=VALUE tokens → emit VERBATIM (no quotes) when the scanner is in
                            an "env context" (= at start, or just after any
                            shell op).  Multiple NAME=VALUE can stack.
      * Everything else   → shell-quote ONLY IF the token (after stripping
                            CMake-level outer "") contains a shell-special
                            char.  Safe plain words / -D options / Windows
                            absolute paths / version numbers are all emitted
                            verbatim.
    """
    SHELL_OPS = {
        "&&", "||", ";", "|", "&",
        "<", ">", ">>", "<<", "<>",
        "1>", "2>", "&>", "1>>", "2>>", "&>>",
        "2>&1", "1>&2", ">&", "<&",
        "2>/dev/null", ">/dev/null",
    }
    _ENV_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
    # Shell-special characters that REQUIRE wrapping the token in single
    # quotes for safe eval by `bash -lc`.  Note: =, :, /, \, -, _, ., +, %
    # are NOT shell specials (they're fine in plain words).
    _SHELL_SPECIAL_CHARS = set(" \t$`!*?#~[](){};&|<>\\'\"")

    def _strip_cmake_outer(tok):
        if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
            return tok[1:-1]
        return tok

    def _shell_quote(value):
        # value is the CMake-stripped token — never has a CMake "" wrapper.
        if value == "":
            return "''"
        if not any(ch in _SHELL_SPECIAL_CHARS for ch in value):
            return value
        inner = value.replace("'", "'\\''")
        return f"'{inner}'"

    pieces = []
    allow_env_prefix = True  # toggled True after every SHELL_OP
    for t in tokens:
        core = _strip_cmake_outer(t)
        if core in SHELL_OPS:
            pieces.append(core)
            allow_env_prefix = True
            continue
        if allow_env_prefix and _ENV_RE.match(core):
            # NAME=VALUE env prefix → emit VERBATIM.  Remain in env-context
            # so consecutive NAME=VALUE tokens stack correctly.
            pieces.append(core)
            continue
        pieces.append(_shell_quote(core))
        allow_env_prefix = False
    return " ".join(pieces)


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
    #
    # v13.0 Strategy 1B: whole-package no-ops.  Certain packages do NOT
    # contribute to a minimal mpv/libmpv build that cares about Blu-ray +
    # DVD playback — and in some cases their presence is actively harmful:
    #
    #  *openssl* → we specifically want SChannel (Win TLS) instead of
    #               OpenSSL so the user's libmpv-2.dll won't ever hit the
    #               OpenSSL assertion crashes that triggered this whole
    #               build.  Short-circuiting every openssl-*-impl.cmake
    #               marks all EP steps as successful (the stamp files still
    #               get touched by the outer *-.cmake wrapper), so downstream
    #               dependency checks pass without a real libssl being built.
    #  *shaderc*manual-install* → shaderc is Vulkan shader compile-at-runtime
    #               support.  Its manual-install step fails L416 because
    #               shaderc_combined.a ends up in a different dir than
    #               superbuild expects.  mpv doesn't need runtime Vulkan
    #               shader compilation — pre-compiled Vulkan shaders work
    #               fine.  Skip whole package to clean up FAILED noise.
    is_cleanup_script = False
    base = (filename_for_context or "").lower().replace("\\", "/").split("/")[-1]
    # 1B whole-package match first (any step in matching packages)
    for pkg_snippet in ("openssl-", "shaderc-", "cppwinrt-"):
        if pkg_snippet in base:
            is_cleanup_script = True
            break
    if not is_cleanup_script and base.endswith("--impl.cmake"):
        stem = base[:-len("--impl.cmake")]
        # v14.0: added more step suffixes that 100% should succeed even when
        # they report exit 1 — they are non-critical housekeeping for which
        # -k 100 already pushes on, but short-circuiting them keeps the
        # FAILED list small and meaningful for the final push to mpv DLL:
        #  *-check-git     → mpv's `git describe` to generate version.h.
        #                     We clone via single_source in a way that doesn't
        #                     guarantee an annotated tag, so git describe may
        #                     exit 1 — mpv will still build without it.
        #  *-10bit-lib-install / *-12bit-lib-install
        #                   → x265 multi-bit-depth install step that copies
        #                     libx265.a (from out-of-tree builds) to the
        #                     combined x265 build dir.  These often fail
        #                     because libx265.a ended up elsewhere, but
        #                     8-bit-only x265 is enough for mpv playback.
        #  *-autoconf       → projects that re-run autoconf/autoreconf on
        #                     old source trees; not every runner has
        #                     autoconf/automake/libtool installed, and
        #                     pre-configured configure scripts usually
        #                     work fine without re-running.
        for _sfx in ("-postremovebuild", "-removebuild", "-download", "-update",
                     "-check-git",
                     "-10bit-lib-install", "-12bit-lib-install",
                     "-autoconf"):
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
                # v12.8-9: wrapping policy.  Upstream ExternalProject already
                # wraps autotools/configure steps as `bash -lc "..."`.  If we
                # re-wrap those in ANOTHER `bash -lc " 'bash' '-lc' '...' "`,
                # single quotes inside the inner command get escaped to `'\''`
                # and passed to inner bash completely mangled — every
                # configure step then exits with code 1.  So first we detect
                # "already wrapped" cases and pass them through verbatim.
                #
                # For anything else (cmake.exe, ninja.exe, PE executables,
                # compound commands that somehow escaped upstream's wrap,
                # extension-less autotools `configure` scripts etc.) we DO
                # wrap once — bash will correctly find PEs on PATH and the
                # 20ms startup cost is the price of never missing a case.
                if not argv:
                    # execute_process(COMMAND) with zero args → leave alone.
                    continue
                # v13.0 Strategy 3: If ANY argv token contains an unexpanded
                # CMake variable reference (${VAR}, $ENV{VAR}) or a generator
                # expression ($<...>), ABORT ALL REWRITING for this COMMAND.
                #
                # Typical external project template:
                #   set(command "bash.exe;-lc;cd /x && ./configure --prefix=/y")
                #   execute_process(COMMAND ${command} RESULT_VARIABLE rc)
                #
                # At patch time argv = ["${command}"] (one element).  If we
                # naively wrap that as `bash -lc "'${command}'"`, the quotes
                # will prevent CMake from splitting the expanded string on
                # `;` (CMake list separator), so CreateProcess ends up
                # looking for an executable literally named
                # "bash.exe;-lc;cd ..." (with semicolons in the name) and
                # exits with "no such file or directory" — which is exactly
                # the failure we saw on gcc-binutils-configure L15.
                #
                # Upstream already chose the exact wrapping (bash -lc) inside
                # that `set(command "...")` line.  We cannot second-guess it
                # statically.  So leave these patterns completely untouched.
                has_cmake_placeholder = False
                for a in argv:
                    if ("${" in a) or ("$ENV{" in a) or ("$<" in a):
                        has_cmake_placeholder = True
                        break
                if has_cmake_placeholder:
                    out.append(" " + " ".join(argv))
                    out.append(" ")  # Separator for next keyword
                    continue
                already_wrapped = False
                head = argv[0] or ""
                # Parser may keep CMake outer double-quotes around the token, so
                # strip them before we take basename for the shell-exec check.
                head_core = head[1:-1] if (len(head) >= 2 and head[0] == '"' and head[-1] == '"') else head
                head_lower = head_core.lower().replace("\\", "/").split("/")[-1]
                if head_lower in ("bash", "bash.exe", "sh", "sh.exe", "dash", "dash.exe"):
                    if len(argv) >= 2:
                        flag = argv[1]
                        flag_core = flag[1:-1] if (len(flag) >= 2 and flag[0] == '"' and flag[-1] == '"') else flag
                        if flag_core in ("-lc", "-c", "--lc", "-Lc"):
                            already_wrapped = True
                if already_wrapped:
                    out.append(" " + " ".join(argv))
                    out.append(" ")  # Separator for next keyword (WORKING_DIRECTORY…)
                    continue
                shell_str = bash_recompose(argv)
                out.append(" bash -lc ")
                escaped = shell_str.replace("\\", "\\\\").replace('"', '\\"')
                out.append(f'"{escaped}"')
                out.append(" ")  # Separator for next keyword (WORKING_DIRECTORY…)
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
    # ------------------------------------------------------------------
    # V20: Expand candidate scope.  The previous scan only covered
    #   *--impl.cmake  files (362), but the build.ninja steps actually
    #   invoke the NON-impl  *-<step>-.cmake  wrappers (e.g.
    #   ffmpeg-configure-.cmake) which ALSO contain
    #       set(command "…/build/exec;…")   +   execute_process(COMMAND ${command})
    #   The Strategy 3 `${command}` placeholder guard SKIPS all of these
    #   because the argv list has a single "${command}" element, leaving
    #   build/exec in command → every configure/build/install returns 0
    #   silently without doing any real work.
    # Fix:  scan every .cmake under  **/*-stamp/  AND every
    #   **/*-stamp/**/*--impl.cmake  (i.e. the union of old scope + new
    #   stamp-dir-scope).
    # ------------------------------------------------------------------
    seen_paths = set()
    all_files = []
    # Old scope: all --impl.cmake anywhere under build/
    for p in build_dir.rglob("*--impl.cmake"):
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp not in seen_paths:
            seen_paths.add(rp)
            all_files.append(p)
    # V20 new scope: any *.cmake directly under a *-stamp/ directory
    for p in build_dir.rglob("*-stamp/*.cmake"):
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp not in seen_paths:
            seen_paths.add(rp)
            all_files.append(p)
    all_files.sort()
    count_patched = 0
    count_s0_fixes = 0
    count_seen = 0
    count_skipped = 0
    print(f"V20: scanning {len(all_files)} candidate cmake files (*--impl.cmake + *-stamp/*.cmake) under {build_dir}")
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
        if "execute_process" not in src and "set(command " not in src:
            continue
        # ---- V20 Strategy 0 FIRST — rewrite set(command "build/exec;…")
        #      so that Strategy 3 ${command} guard downstream no longer
        #      prevents wrapping.
        dst, s0_count = _patch_setcommand_buildexec(src)
        changed = (dst != src)
        count_s0_fixes += s0_count
        # ---- Fallback to Strategy 1/2 (execute_process-level rewriting)
        #      for any COMMAND that Strategy 0 did not cover.
        try:
            dst2 = patch_execute_process(dst, filename_for_context=str(p))
        except Exception as e:
            print(f"[SKIP:parse] {p}: {type(e).__name__}: {e}", file=sys.stderr)
            count_skipped += 1
            continue
        if dst2 != dst:
            changed = True
            dst = dst2
        if changed:
            try:
                # Write via bytes to avoid Windows newlines mangling CMake args.
                p.write_bytes(dst.encode("utf-8"))
            except OSError as e:
                print(f"[SKIP:write] {p}: {e}", file=sys.stderr)
                count_skipped += 1
                continue
            count_patched += 1
            rel = p.relative_to(build_dir).as_posix()
            extra = f" [S0={s0_count}]" if s0_count else ""
            print(f"   patched: {rel}{extra}")
    print(f"\nV25 DONE patch_impl_cmakes.py: scanned {count_seen} cmake files, "
          f"patched {count_patched} (Strategy0 build/exec -> bash -c kScript@abs fixes={count_s0_fixes}), "
          f"bash resolved to: {BASH_EXE_ABS!r}; skipped {count_skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
