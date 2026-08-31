/*
 * exec_wrapper.c  —  shinchiro mpv-winbuild-cmake superbuild launcher replacement.
 *
 * WHY (v12.3 → v12.4 evolution):
 *   v12.3 PE wrapper was:  bash /path/to/exec.sh <user-argv...>
 *   That worked for CreateProcess / PE image format (no more ERROR_BAD_EXE_FORMAT).
 *   But 3 call-shapes in the superbuild still died with bash exit 126/127:
 *     (A)  exec  D:/.../mingw32/bin/cmake.exe  -H... -B...
 *               → MSYS2 bash's execve of a Windows-drive absolute path sometimes
 *                 returns "Exec format error" (126), even though the file is a valid PE.
 *     (B)  exec  'make'  '-j4'  'PREFIX=/i686'  'install'
 *               → exit 126.
 *     (C)  exec  'cd'  '<dir>'  '&&'  'CONF=1'  './configure'  '--host=i686'
 *          exec  'CONF=1'  'PATH=$O_PATH'  'cmake'  ...
 *               → argv[0] = 'cd' / 'CONF=1'  →  direct execvp(argv[0], …) fails:
 *                   'cd' is a shell builtin (no ELF/PE to exec)  →  126
 *                   'CONF=1' isn't a file on PATH              →  127 "command not found".
 *
 *   All three shapes are **shell compound statements / shell-assignment prefixes**,
 *   not just "prog + args".  The old bash launcher worked when it was invoked FROM a
 *   bash shell (not via CreateProcess) because the user's invoking shell already
 *   parsed `&&` / `VAR=value` as shell syntax.  We lost that layer when we had to
 *   wrap the entrypoint in a PE for CreateProcess.
 *
 * CORRECT FIX (this file):
 *   Build a `bash -c` invocation that:
 *     1. Saves incoming user argv into a bash array  _CMD=("$@")
 *     2. `set --`   (empties positional params,  so  `exec.sh`'s trailing `"$@"`
 *                    becomes a no-op when sourced instead of re-exec-ing the raw
 *                    tokens the "wrong" way)
 *     3. `. "$0"`   (sources exec.sh — PATH, O_PATH, any other env now in effect)
 *     4. `eval "$(printf ' %q' "${_CMD[@]}")"`
 *                    — round-trips every user argv through bash's `%q` (safe re-quoting
 *                      of spaces and shell metachars), then eval-s the concatenated
 *                      result as a normal shell command line.
 *                    → cd / && / VAR=prefix / pipe / redirect all work again.
 *                    → For case (A) D:/.../cmake.exe:  `eval` on that string inside bash
 *                      re-parses it through bash's command-lookup path (not raw execve
 *                      of the first token), so MSYS2 resolves the Win32 absolute path
 *                      correctly.
 *
 * BUILD:   i686-w64-mingw32-gcc -Os -m32 -Wall -Wextra -s exec_wrapper.c -o build/exec
 *          (no extension — matches CreateProcess exact-path call site).
 */

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <process.h>
#include <errno.h>

/*
 * The bash one-liner we actually execute.
 *
 * When invoked as:   bash  -c  SCRIPT  EXEC_SH_PATH  USER_ARGV1  USER_ARGV2 ...
 * bash sets:
 *     $0 = EXEC_SH_PATH     (used by  `. "$0"`  to source the original launcher)
 *     $1 = USER_ARGV1
 *     $2 = USER_ARGV2   etc.
 * so `_CMD=("$@")` on the first line captures exactly USER_ARGV[1..n].
 */
static const char kScript[] =
    "_CMD=(\"$@\"); "
    "set --; "
    ". \"$0\"; "
    "eval \"$(printf ' %%q' \"${_CMD[@]}\")\"";

int main(int argc, char **argv)
{
    char self[MAX_PATH];
    char script[MAX_PATH];
    char *lastsep;
    char **nargv;
    int i, n;
    intptr_t rc;

    if (!GetModuleFileNameA(NULL, self, (DWORD)MAX_PATH)) {
        DWORD gle = GetLastError();
        fprintf(stderr,
                "exec-wrapper: GetModuleFileNameA failed, error=%lu\n",
                (unsigned long)gle);
        return 127;
    }
    self[MAX_PATH - 1] = '\0';

    /* Replace basename of own EXE path with exec.sh */
    strncpy(script, self, MAX_PATH - 1);
    script[MAX_PATH - 1] = '\0';
    lastsep = strrchr(script, '\\');
    if (!lastsep) lastsep = strrchr(script, '/');
    if (lastsep) {
        strcpy(lastsep + 1, "exec.sh");
    } else {
        strcpy(script, "exec.sh");
    }

    /*
     * Build the argv vector for _spawnvp("bash", …):
     *   nargv[0]             = "bash"
     *   nargv[1]             = "-c"
     *   nargv[2]             = <kScript>
     *   nargv[3]             = <path-to-exec.sh>   → bash fills this as $0 in -c script
     *   nargv[4 .. 4+argc-2] = argv[1 .. argc-1]   → user command tokens
     *   nargv[4 + argc - 1]  = NULL
     * Note: original argc counts our own argv[0].  We drop our argv[0], so user
     *       token count = argc - 1.  Total nargv slots = 1 (bash) + 2 (-c, script)
     *       + 1 (exec.sh / $0) + (argc - 1) + 1 (NULL) = argc + 4.
     */
    n = argc + 4;
    nargv = (char **)calloc((size_t)n, sizeof(char *));
    if (!nargv) {
        fprintf(stderr, "exec-wrapper: out of memory allocating argv\n");
        return 127;
    }
    nargv[0] = "bash";
    nargv[1] = "-c";
    /* cast: _spawnvp signature takes (const char * const *), non-const strings
     * inside are OK (they are only read by bash). */
    nargv[2] = (char *)kScript;
    nargv[3] = script;
    for (i = 1; i < argc; i++) {
        nargv[3 + i] = argv[i];
    }
    nargv[3 + argc] = NULL;

    rc = _spawnvp(_P_WAIT, "bash", (const char * const *)nargv);
    if (rc < 0) {
        int e = errno;
        const char *path_env = getenv("PATH");
        fprintf(stderr,
                "exec-wrapper: _spawnvp(_P_WAIT, \"bash\", bash -c '…' \"%s\" …) "
                "failed: errno=%d\n",
                script, e);
        fprintf(stderr,
                "exec-wrapper: own_exe  = %s\n"
                "exec-wrapper: exec.sh  = %s\n"
                "exec-wrapper: argc     = %d (user tokens=%d)\n"
                "exec-wrapper: PATH=%%s = %s\n",
                self, script, argc, argc - 1,
                path_env ? path_env : "(null)");
        for (i = 0; i < argc && i < 32; i++) {
            fprintf(stderr, "exec-wrapper: user_argv[%2d] = %s\n", i, nargv[4 + i]);
        }
    }

    free(nargv);
    return (rc < 0) ? 127 : (int)rc;
}
