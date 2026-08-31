/*
 * exec_wrapper.c  —  shinchiro mpv-winbuild-cmake superbuild launcher replacement.
 *
 * WHY:  On Windows (native cmd.exe / Windows-native Ninja), the superbuild writes a
 *       helper launcher `<build>/exec` as a *bash shebang script*.  The helper sets
 *       PATH / O_PATH then eval-s the arguments.  But all CMake ExternalProject steps
 *       invoke it via CreateProcess with an EXACT FULL PATH and NO extension:
 *
 *           CreateProcess("D:/a/.../build/exec", "cmake -H... -B...", ...)
 *
 *       CreateProcess with a full, extension-less path treats the file as a PE image
 *       (it does NOT consult PATHEXT), so a bash script always dies with
 *           ERROR_BAD_EXE_FORMAT / "%1 is not a valid Win32 application"
 *       which superbuild translates to: "Command failed: inappropriate file type or format"
 *
 *       (see build.yml job-logs.txt v12.2 — EVERY ExternalProject step failed this way).
 *
 * THIS FIX:
 *       1. Rename the original bash-script launcher from `<build>/exec` → `<build>/exec.sh`.
 *       2. Compile THIS C source with mingw32-gcc into `<build>/exec` (no extension):
 *              i686-w64-mingw32-gcc -Os -m32 exec_wrapper.c -o exec
 *       3. The resulting PE locates its own EXE path, replaces the trailing file name
 *          with "exec.sh", then spawns `bash <exec.sh> <original argv...>` via PATH lookup.
 *
 *       Result: CreateProcess loads a valid PE image (MZ), bash still runs exactly the
 *               original launcher (PATH / O_PATH env intact), and all 1290 superbuild
 *               steps can proceed.
 *
 * BUILD (mingw):   i686-w64-mingw32-gcc -Os -m32 exec_wrapper.c -o exec
 *                  gcc           -Os -m32 exec_wrapper.c -o exec   (on MSYS2 mingw32 shell)
 */

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <process.h>
#include <errno.h>

int main(int argc, char **argv)
{
    char self[MAX_PATH];
    char script[MAX_PATH];
    char *lastsep;
    char **nargv;
    int i;
    intptr_t rc;   /* _spawnvp returns intptr_t on msvcrt/ucrt */

    if (!GetModuleFileNameA(NULL, self, (DWORD)MAX_PATH)) {
        DWORD gle = GetLastError();
        fprintf(stderr,
                "exec-wrapper: GetModuleFileNameA failed, error=%lu\n",
                (unsigned long)gle);
        return 127;
    }
    self[MAX_PATH - 1] = '\0';

    /* start with a copy of own path, then replace the basename with exec.sh */
    strncpy(script, self, MAX_PATH - 1);
    script[MAX_PATH - 1] = '\0';
    lastsep = strrchr(script, '\\');
    if (!lastsep) lastsep = strrchr(script, '/');
    if (lastsep) {
        strcpy(lastsep + 1, "exec.sh");
    } else {
        /* no directory component at all (shouldn't happen, but be safe) */
        strcpy(script, "exec.sh");
    }

    /* nargv = [ script, argv[1], argv[2], ..., NULL ]
     * _spawnvp(mode, "bash", argv)  will actually run:   bash script argv[1] argv[2] ...
     * which is exactly what we want. */
    nargv = (char **)calloc((size_t)argc + 1, sizeof(char *));
    if (!nargv) {
        fprintf(stderr, "exec-wrapper: out of memory allocating argv\n");
        return 127;
    }
    nargv[0] = script;
    for (i = 1; i < argc; i++) {
        nargv[i] = argv[i];
    }
    nargv[argc] = NULL;

    /* PATH lookup for `bash` — msys2/setup-msys2 puts /usr/bin/bash.exe on PATH
     * already inside the msys2 shell, AND also exposes that bash.exe to native
     * Windows PATH (it's copied into a staging dir).  If _spawnvp fails, we dump
     * PATH so the workflow log immediately shows why. */
    rc = _spawnvp(_P_WAIT, "bash", (const char * const *)nargv);
    if (rc < 0) {
        int e = errno;
        const char *path_env = getenv("PATH");
        fprintf(stderr,
                "exec-wrapper: _spawnvp(_P_WAIT, \"bash\", \"%s\", ...) failed: errno=%d\n",
                script, e);
        fprintf(stderr,
                "exec-wrapper: own_exe   = %s\n"
                "exec-wrapper: exec.sh   = %s\n"
                "exec-wrapper: argc      = %d\n"
                "exec-wrapper: PATH=%%s  = %s\n",
                self, script, argc,
                path_env ? path_env : "(null)");
        for (i = 0; i < argc && i < 32; i++) {
            fprintf(stderr, "exec-wrapper: argv[%2d] = %s\n", i, nargv[i]);
        }
    }

    free(nargv);
    return (rc < 0) ? 127 : (int)rc;
}
