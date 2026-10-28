# Run a tests/bn_*.py check script on Binary Ninja in this process (bn-script-bridge's bnengine; the bnrun bridge and
# the shared app are gone since 2026-10-28, jvm-113):
#   /Users/patrick/dev/bn-script-bridge/bn tests/bn_exec.py tests/bn_shapes_check.py [NAME=VALUE ...]
# NAME=VALUE pairs (Python literals: SHOW=True, CLASSES='["a/B.class"]') become the script's globals, like the lines
# bnrun scripts got prepended (a file with such lines at its top, e.g. the dump batches, works as well). The installed
# plugin (this repo, symlinked into BN's plugin directory) loads with the engine as package `binary-jvm`, so every run
# sees the working tree: no dev load (tests/bn_dev_load.py was for the shared app). Each run is its own process:
# parallel batches are just several of these side by side. Timeout: `bn --timeout N tests/bn_exec.py ...` (exit 124).
# update_analysis_and_wait() works on any thread since bn-script-bridge d47734e (before it, not on the main thread).
import ast
import os
import runpy
import sys

sys.path.insert(0, "/Users/patrick/dev/bn-script-bridge")
import bnengine  # noqa: E402

if len(sys.argv) < 2:
    sys.exit("usage: bn tests/bn_exec.py SCRIPT [NAME=VALUE ...]")
bnengine.engine()
script, init = sys.argv[1], {}
for pair in sys.argv[2:]:
    name, _, value = pair.partition("=")
    init[name] = ast.literal_eval(value)
sys.argv = [script]

runpy.run_path(script, init_globals=init, run_name="__main__")
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)  # (no engine teardown on exit; a failing script raised / SystemExit-ed above with its status)
