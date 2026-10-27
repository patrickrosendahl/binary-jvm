"""pyflakes over the plugin and tests/ (jvm-108, after binary-cpp's suite.py lint, bncpp-133).

A redefined function or an undefined name in pseudo_java.py is silent until a BN dump shows the damage, so the
gates (vineflower_compare.py, vineflower_suite.py compare, class_view_compare.py) run this first and fail on a
finding. The bridge scripts in tests/ get bv / bvs / env from bnrun, so they count as builtins there; their other
settings (CLASSES, OUT_DIR, DEV_PKG, ...) are read with globals().get(...).

usage: python3 tests/lint.py     # exit 1 on a finding
"""
import os, shutil, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINT = [([".", "shared"], ""), (["tests"], "bv,bvs,env")]  # (paths, builtins); "." = the plugin package (top-level *.py)


def lint(quiet=False):
    """the number of pyflakes findings (printed), -1 without pyflakes"""
    exe = shutil.which("pyflakes")
    cmd = [exe] if exe else [sys.executable, "-m", "pyflakes"]
    found = 0
    for paths, builtins in LINT:
        files = []
        for p in paths:
            if p == ".":
                files += sorted(f for f in os.listdir(REPO) if f.endswith(".py"))
            else:
                files.append(p)
        r = subprocess.run(cmd + files, cwd=REPO, capture_output=True, text=True,
                           env=dict(os.environ, PYFLAKES_BUILTINS=builtins))
        if r.returncode and not r.stdout and "No module named pyflakes" in r.stderr:
            print("lint: pyflakes not found (pip install pyflakes)")
            return -1
        lines = (r.stdout + r.stderr).splitlines()
        for line in lines:
            print(line)
        found += len(lines)
    if found or not quiet:
        print("lint: %d finding(s)" % found)
    return found


if __name__ == "__main__":
    sys.exit(int(lint() != 0))
