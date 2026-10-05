# Run inside Binary Ninja via the script bridge, after tests/bn_dev_load.py:
#   bnrun tests/bn_batch_check.py
# bnrun scripts cannot be interrupted -- keep LIMIT small (default 20 classes).
# For each .class: create the dev JVM view, analyze, and report functions whose stack pointer becomes
# undetermined or whose HLIL fails (set CHECK_LLIL = True to also scan LLIL for `unimplemented`).
import os, time
import binaryninja as b
CLASS_DIR = globals().get("CLASS_DIR", "/Users/patrick/dev/binary-jvm/sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")
LIMIT = globals().get("LIMIT", 20)
CHECK_LLIL = globals().get("CHECK_LLIL", False)
vt = b.BinaryViewType[b._jvm_dev["ns"]["VIEW_NAME"]]
paths = sorted(os.path.join(d, f) for d, _, fs in os.walk(CLASS_DIR) for f in fs if f.endswith(".class"))[:LIMIT]
stats = {"classes": 0, "functions": 0, "unimpl": [], "sp": [], "hlil": [], "fail": []}
t0 = time.time()
for p in paths:
    t = time.time()
    raw = b.BinaryView.open(p)
    v = vt.create(raw) if raw else None
    if v is None:
        stats["fail"].append(p); print("FAIL %s" % p); continue
    v.update_analysis_and_wait()
    stats["classes"] += 1
    for f in v.functions:
        stats["functions"] += 1
        name = "%s:%s" % (os.path.basename(p), f.name)
        try:
            if CHECK_LLIL and any(i.operation == b.LowLevelILOperation.LLIL_UNIMPL for i in f.llil.instructions):
                stats["unimpl"].append(name)
            if any(f.get_reg_value_at(blk.start, "s").type == b.RegisterValueType.UndeterminedValue for blk in f.basic_blocks):
                stats["sp"].append(name)
            if f.hlil is None or f.hlil.root is None:
                stats["hlil"].append(name)
        except Exception as e:
            stats["hlil"].append("%s %r" % (name, e))
    print("%5.2fs %3d funcs %s" % (time.time()-t, len(v.functions), os.path.relpath(p, CLASS_DIR)))
    v.file.close()
print("classes %d, functions %d, %.1fs total" % (stats["classes"], stats["functions"], time.time()-t0))
for k in ("fail", "unimpl", "sp", "hlil"):
    print(k, len(stats[k]))
    for x in stats[k][:15]: print("   ", x)
