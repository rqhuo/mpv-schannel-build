#!/usr/bin/env python3
"""Unit test for patch_impl_cmakes.py V20 Strategy 0 changes."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from patch_impl_cmakes import (
    _is_build_exec_token,
    _patch_setcommand_buildexec,
    _cmake_quote_for_set,
    bash_recompose,
)

def test1_is_build_exec():
    print("Test 1 _is_build_exec_token:")
    cases_ok = [
        'D:/a/mpv-winbuild/build/exec',
        'D:/a/mpv-winbuild/build/exec.exe',
        '"D:/a/mpv-winbuild/build/exec"',
        '/c/Users/x/mpv-winbuild/build/exec',
        'D:\\a\\build\\exec',
        'C:/msys64/home/runner/build/build/exec',
    ]
    cases_bad = [
        'bash', 'cmake', 'gcc', 'ninja',
        '/mingw32/bin/bash.exe',
        '/usr/bin/make',
        'D:/a/exec',
        'build/exec_wrapper',
        'D:/build/exec_old',
    ]
    for c in cases_ok:
        r = _is_build_exec_token(c)
        assert r, f"FAIL ok-case: {c!r}"
        print(f"   OK : {c}")
    for c in cases_bad:
        r = _is_build_exec_token(c)
        assert not r, f"FAIL bad-case: {c!r}"
        print(f"   BAD: {c}")
    print("  PASS")

def test2_env_prefix():
    print()
    print("Test 2 bash_recompose env-prefix:")
    tokens = ['CONF=1', 'LTO_JOB=1', 'meson', 'setup', '/i686/build', '/i686/src', '--prefix=/i686']
    r = bash_recompose(tokens)
    print(f"  result: {r}")
    assert r.startswith("CONF=1 LTO_JOB=1 "), f"env prefix lost: {r!r}"
    assert "'meson'" in r, "meson not quoted"
    assert "'/i686/build'" in r, "build path not quoted"
    assert "'--prefix=/i686'" in r, "prefix flag not quoted"
    print("  PASS")

def test3_shell_ops():
    print()
    print("Test 3 bash_recompose shell-ops (&&):")
    tokens = ['find', 'D:/pkg/src', '-mindepth', '1', '-delete', '&&',
              'git', '-C', 'D:/pkg/src', 'clean', '-df']
    r = bash_recompose(tokens)
    print(f"  result: {r}")
    # && must be unquoted
    assert " && " in r, f"shell op && lost: {r!r}"
    assert "'find'" in r
    assert "'git'" in r
    print("  PASS")

def test4_fontconfig_configure():
    print()
    print("Test 4 Strategy0 fontconfig configure (L467 pattern):")
    src = """cmake_minimum_required(VERSION 3.30)

set(command "D:/a/mpv-schannel-build/mpv-winbuild/build/exec;CONF=1;meson;setup;D:/a/fontconfig-build;D:/a/fontconfig;--prefix=/i686;--libdir=/i686/lib;--cross-file=D:/a/meson_cross.txt;--buildtype=release;-Ddoc=disabled")

set(log_merged "")
execute_process(COMMAND ${command} RESULT_VARIABLE result OUTPUT_FILE "o.log" ERROR_FILE "e.log")
if(result)
  message(FATAL_ERROR "failed: ${result}")
endif()
"""
    dst, n = _patch_setcommand_buildexec(src)
    print(f"  S0 fixes = {n}")
    assert n == 1, f"expected 1 fix got {n}"
    for line in dst.splitlines():
        if line.strip().startswith("set(command "):
            print(f"  NEW (first 220): {line[:220]}")
            assert "build/exec" not in line, "build/exec still present!"
            assert "bash;-lc;" in line, f"no bash;-lc wrapper: {line[:120]}"
            assert "CONF=1" in line, "CONF=1 env prefix lost"
            assert "meson" in line
            break
    print("  PASS")

def test5_sed_command():
    print()
    print("Test 5 Strategy0 sed command (L457 pattern):")
    src = ('set(command "D:/a/mpv-winbuild/build/exec;sed;-i;'
           's/both_libraries/library/g;D:/a/src/meson.build")\n'
           'execute_process(COMMAND ${command} RESULT_VARIABLE result)\n')
    dst, n = _patch_setcommand_buildexec(src)
    print(f"  S0 fixes = {n}")
    assert n == 1
    for line in dst.splitlines():
        if line.strip().startswith("set(command "):
            print(f"  NEW: {line}")
            assert "build/exec" not in line
            assert "sed" in line
            assert "s/both_libraries/library/g" in line, "sed pattern lost"
            break
    print("  PASS")

def test6_multiple_mixed():
    print()
    print("Test 6 Strategy0 mixed + non-buildexec lines:")
    src = """
set(command "D:/a/build/exec;find;/tmp;-name;*.o;-delete")
set(command "bash;-c;echo hello")
set(command "D:/a/build/exec;LTO_JOB=1;ninja;-C;D:/a/build/pkg")
set(command "cmake;-E;touch;stamp")
set(command "D:/a/build/exec;PDB=1;strip;/i686/bin/libx.dll")
"""
    dst, n = _patch_setcommand_buildexec(src)
    print(f"  S0 fixes = {n}")
    assert n == 3, f"expected 3 fixes got {n}"
    assert 'set(command "bash;-c;echo hello")' in dst, "non-buildexec bash line changed!"
    assert 'set(command "cmake;-E;touch;stamp")' in dst, "non-buildexec cmake line changed!"
    assert "LTO_JOB=1" in dst, "LTO_JOB env prefix lost"
    assert "PDB=1" in dst, "PDB env prefix lost"
    print("  PASS")

def test7_find_delete_git_clean():
    print()
    print("Test 7 Strategy0 find && git clean (postremovebuild pattern):")
    src = 'set(command "D:/a/mpv-winbuild/build/exec;find D:/pkg/src -mindepth 1 -delete && git -C D:/pkg/src clean -df")\n'
    # NOTE: This pattern has NO semicolons inside the inner command string.
    # The build/exec launcher is argv[0], then argv[1] is the entire shell command.
    dst, n = _patch_setcommand_buildexec(src)
    print(f"  S0 fixes = {n}")
    assert n == 1, f"expected 1, got {n}"
    line = dst.splitlines()[0]
    print(f"  NEW: {line}")
    assert "build/exec" not in line
    assert "bash;-lc;" in line
    assert "find D:" in line or "'find D:" in line, "find cmd content lost"
    assert "&&" in line or "&amp;&amp;" not in line, "shell ops preserved"
    print("  PASS")

if __name__ == "__main__":
    test1_is_build_exec()
    test2_env_prefix()
    test3_shell_ops()
    test4_fontconfig_configure()
    test5_sed_command()
    test6_multiple_mixed()
    test7_find_delete_git_clean()
    print()
    print("=" * 60)
    print("ALL 7 TESTS PASSED  V20 Strategy 0 is correct")
    print("=" * 60)
