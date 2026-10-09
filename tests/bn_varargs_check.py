# Run inside Binary Ninja via the script bridge (installed plugin, or after tests/bn_dev_load.py):
#   bnrun --timeout 300 tests/bn_varargs_check.py          (prepend DEV_PKG = "jvm_devN" for a dev load)
# jvm-64: varargs callees declared next to their caller (tests/synthetic/, built by tests/synthetic/build.sh).
# Pseudo Java reads ACC_VARARGS from the callee's class file in the same directory (jvm-57); every caller
# method must contain its expected text and none of the forbidden ones. Prints PASS / FAIL per case and
# exits non-zero when a case fails (jvm-94).
import os, sys
import binaryninja as b

REPO = "/Users/patrick/dev/binary-jvm"
BUILDS = ["classes", "classes21"]  # javac --release 8 / 21 (tests/synthetic/build.sh)
PKG = globals().get("DEV_PKG") or (b._jvm_dev["pkg"].__name__ if hasattr(b, "_jvm_dev") else "binary-jvm")
CASES = {  # method: (expected substrings, forbidden substrings); parameters are named arg1.. (no debug info)
    "spread": (['Varargs.join(",", "a", "b", "c")'], ["new String[]"]),
    "none": (['Varargs.join(";")'], ["new String[0]"]),
    "explicitArray": (['Varargs.join("-", arg1)'], []),
    "instance": (["arg1.sum(1, 2, 3)"], ["new int[]"]),
    "overloaded": (['arg1.tag(new Object[]{"x"})'], []),  # tag("x") would call tag(String)
    "overloadedSpread": (["arg1.tag(1, 2)"], ["new Object[]"]),
    "nullArray": (['Varargs.join("+", (String[]) null)'], []),  # the array itself, null
    "notVarargs": (['Varargs.count(new String[]{"p", "q"})'], []),
}
pj = sys.modules[PKG + ".pseudo_java"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
failed = 0
for build in BUILDS:
    v = vt.create(b.BinaryView.open(os.path.join(REPO, "tests/synthetic", build, "jvmtest/VarargsCaller.class")))
    v.update_analysis_and_wait()
    todo = dict(CASES)
    for f in sorted(v.functions, key=lambda f: f.start):
        name = f.name.split("(")[0]
        if name not in todo:
            continue
        text = "\n".join(pj.render_method(f))
        want, never = todo.pop(name)
        bad = [w for w in want if w not in text] + ["unwanted: " + n for n in never if n in text]
        failed += bool(bad)
        print("%-4s %-9s %-17s %s" % ("FAIL" if bad else "PASS", build, name, bad or ""))
        if bad:
            print("\n".join("       " + l for l in text.splitlines()))
    for name in todo:
        failed += 1
        print("FAIL %-9s %-17s no such function" % (build, name))
    v.file.close()
print("RESULT", "FAIL %d" % failed if failed else "PASS")
if failed:
    raise SystemExit("%d case(s) failed" % failed)
