"""Shared pytest constants.

The one thing here is BASH. On Windows, `subprocess.run(["bash", ...])` with no
`shell=` hands the name to CreateProcess, which walks PATH in order. On the
GitHub `windows-latest` image, `C:\\Windows\\System32` sits ahead of
`C:\\Program Files\\Git\\bin` -- and System32 contains `bash.exe`, the WSL
launcher stub. It wins the lookup, finds no installed distribution, prints its
help text in UTF-16LE, and exits 1. `scripts/build.sh` never runs, and every
test that shells out fails for a reason that has nothing to do with the code
under test.

Installing a WSL distribution would not fix it -- it would make it worse. These
call sites pass Windows absolute paths (`D:\\a\\repo\\scripts\\build.sh`), which
do not exist inside WSL's filesystem view; they would have to be `/mnt/d/...`.
The right interpreter is Git Bash: it ships with Git for Windows, is already
present on the runner, and is the MSYS2 bash these `#!/usr/bin/env bash`
scripts were written against.

So: resolve the interpreter explicitly instead of trusting PATH order.
"""
import shutil
import sys

_GIT_BASH_DIRS = (
    r"C:\Program Files\Git\bin",
    r"C:\Program Files\Git\usr\bin",
    r"C:\Program Files (x86)\Git\bin",
)


def _resolve_bash():
    if sys.platform == "win32":
        for d in _GIT_BASH_DIRS:
            found = shutil.which("bash", path=d)
            if found:
                return found
        # Fall back to PATH, but reject the System32 WSL stub: failing loudly
        # beats failing as a mangled UTF-16 build error 26 tests deep.
        found = shutil.which("bash")
        if found and "system32" not in found.lower():
            return found
        raise RuntimeError(
            "No usable bash found. The only `bash` on PATH is the WSL launcher "
            "stub in System32, which cannot run these scripts. Install Git for "
            "Windows (which provides Git Bash), or add its bin directory to PATH."
        )
    return shutil.which("bash") or "bash"


#: Absolute path to a usable bash. Use this instead of the bare string "bash"
#: in every subprocess call, or Windows picks the WSL stub.
BASH = _resolve_bash()
