# Run inside Binary Ninja via the script bridge, after tests/bn_dev_load.py:
#   bnrun --parallel --timeout 300 tests/bn_rare_ops.py
# Prepend JVM_REPO = "<checkout>" (and DEV_PKG = "jvm_devN" if a newer dev load may have run).
# Lifts tests/synthetic/classes/jvmtest/RareOps.class (jvm-37): nop, swap, goto_w, jsr_w, wide.
import os, sys
import binaryninja as b

REPO = globals().get("JVM_REPO", "/Users/patrick/dev/binary-jvm")
PKG = globals().get("DEV_PKG") or (b._jvm_dev["pkg"].__name__ if hasattr(b, "_jvm_dev") else None)
if not PKG:
    raise SystemExit("FAIL bn_dev_load.py has not registered a dev view")
path = os.path.join(REPO, "tests/synthetic/classes/jvmtest/RareOps.class")
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
raw = b.BinaryView.open(path)
v = vt.create(raw) if raw else None
if v is None:
    raise SystemExit("FAIL could not open %s" % path)
v.update_analysis_and_wait()
want = ["nop", "swapInts", "gotoWide", "jsrWide", "wideInts", "wideLong", "wideFloat", "wideDouble", "wideRef", "wideRet"]
names = [f.name for f in v.functions]
print("functions", len(v.functions), sorted(names))
failed = []
for f in sorted(v.functions, key=lambda f: f.start):
    llil = f.llil
    if llil is None or any(i.operation == b.LowLevelILOperation.LLIL_UNIMPL for i in llil.instructions):
        failed.append("unimpl " + f.name)
    if f.hlil is None or f.hlil.root is None:
        failed.append("hlil " + f.name)
    print("--- %s ---" % f.name)
    if llil is not None:
        for i in llil.instructions:
            print(" ", i)
missing = [n for n in want if n not in names]
if missing:
    failed.append("missing " + " ".join(missing))
v.file.close()
print("FAIL" if failed else "PASS", failed)
if failed:
    raise SystemExit(1)
