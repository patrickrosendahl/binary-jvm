# Run inside Binary Ninja via the script bridge (installed plugin, or after tests/bn_dev_load.py):
#   bnrun --parallel --timeout 300 tests/bn_anon_check.py      (prepend DEV_PKG = "jvm_devN" for a dev load)
# jvm-66 / jvm-69: anonymous classes printed at their new in the class view (tests/synthetic/, jvmtest.Anon,
# built by tests/synthetic/build.sh). The class text must contain every expected snippet and none of the
# forbidden ones; javac 21 drops an unused this$0, javac 8 keeps it. Prints PASS / FAIL per build and
# exits non-zero when one fails (jvm-94: a raise reaches bnrun as ok=false -> exit 1, stdout kept).
import os, sys
import binaryninja as b

REPO = "/Users/patrick/dev/binary-jvm"
BUILDS = ["classes", "classes21"]  # javac --release 8 / 21 (tests/synthetic/build.sh)
PKG = globals().get("DEV_PKG") or (b._jvm_dev["pkg"].__name__ if hasattr(b, "_jvm_dev") else "binary-jvm")
EXPECTED = [
    "Anon.this.bump();",                          # outer method through this$0
    "Anon.this.count = Anon.this.count + 2;",     # outer field
    "System.out.println(arg2 + Anon.this.count);", # captured local (val$label); no "" + (javac 21 indy, jvm-75)
    "System.out.println(arg2);",                  # anonymous inside anonymous: this$1.val$n
    'return new Thread("t-" + arg2) {',           # super constructor argument, no extra parentheses
    "System.out.println(arg1);",                  # static context: no outer instance
]
FORBIDDEN = ["this$0", "this$1", "val$", "class Anon$", "// anonymous class", "new Anon$"]
classui = sys.modules[PKG + ".classui"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
failed = 0
for build in BUILDS:
    v = vt.create(b.BinaryView.open(os.path.join(REPO, "tests/synthetic", build, "jvmtest/Anon.class")))
    v.update_analysis_and_wait()
    text = "\n".join(classui.render_class(v))
    v.file.close()
    missing = [e for e in EXPECTED if e not in text]
    present = [f for f in FORBIDDEN if f in text]
    if missing or present:
        failed += 1
        print("FAIL %s: missing %r, forbidden %r" % (build, missing, present))
        print(text)
    else:
        print("PASS %s" % build)
print("failed: %d" % failed)
if failed:
    raise SystemExit("%d build(s) failed" % failed)
