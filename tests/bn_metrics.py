# Run inside Binary Ninja via the script bridge, after tests/bn_dev_load.py:
#   bnrun --timeout 600 tests/bn_metrics.py         (prepend START = ..., LIMIT = ... to pick a slice)
# Readability metrics over a slice of the mdg classes with the latest dev view: HLIL lines with .d/.q
# stack-slot accessors, `$catch_` functions, functions with only int32_t-typed parameters, per-class
# analysis time. DUMP = ["Class.method", ...] prints those functions' HLIL.
import os, re, time
import binaryninja as b
CLASS_DIR = globals().get("CLASS_DIR", "/Users/patrick/dev/binary-jvm/sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")
START = globals().get("START", 0)
LIMIT = globals().get("LIMIT", 40)
DUMP = globals().get("DUMP", [])
SHOW = globals().get("SHOW", 0)  # print up to SHOW accessor lines per class
FIELDREF = re.compile(r"&[\w/$<>]+\.[\w$<>]+")  # &pkg/Class.field (a field may be called d or q)
ACCESSOR = re.compile(r"[\w\]\)](:\d+)?(?<!sx)(?<!zx)\.[dq]\b(?!\()")  # var.d / var:4.q, not sx.q(...) or a method X.d(...)
vt = b.BinaryViewType[globals().get("VIEW", b._jvm_dev["ns"]["VIEW_NAME"])]  # VIEW = "JVM Class devN": pin your own dev load (other sessions load too)
paths = sorted(os.path.join(d, f) for d, _, fs in os.walk(CLASS_DIR) for f in fs if f.endswith(".class"))
paths = paths[START:START + LIMIT]
if globals().get("FILES"):  # FILES = ["com/x/Y.class", ...] relative to CLASS_DIR instead of a slice
    paths = [os.path.join(CLASS_DIR, f) for f in FILES]
tot = {"classes": 0, "functions": 0, "accessor_lines": 0, "accessor_classes": 0, "catch_funcs": 0, "time": 0.0, "hlil_fail": 0}
for p in paths:
    t = time.time()
    v = vt.create(b.BinaryView.open(p))
    v.update_analysis_and_wait()
    dt = time.time() - t
    cname = os.path.basename(p)[:-6]
    lines = 0
    for f in v.functions:
        tot["functions"] += 1
        if "$catch_" in f.name:
            tot["catch_funcs"] += 1
        try:
            text = [str(l) for l in f.hlil.root.lines] if f.hlil is not None else None
        except Exception:
            text = None
        if text is None:
            tot["hlil_fail"] += 1
            if SHOW:
                print("   %s: no HLIL (analysis skipped: %r)" % (f.name, getattr(f, "analysis_skip_reason", None)))
            continue
        hits = [l for l in text if ACCESSOR.search(FIELDREF.sub("", l))]
        lines += len(hits)
        for l in hits[:SHOW]:
            print("   %s: %s" % (f.name, l.strip()))
        if "%s.%s" % (cname, f.name) in DUMP or "%s.*" % cname in DUMP:
            print("==== %s.%s" % (cname, f.name))
            print("\n".join(text))
    tot["classes"] += 1
    tot["time"] += dt
    tot["accessor_lines"] += lines
    tot["accessor_classes"] += 1 if lines else 0
    print("%6.2fs %4d acc %s" % (dt, lines, cname))
    v.file.close()
print(tot)
