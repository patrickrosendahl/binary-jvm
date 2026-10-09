# Run inside Binary Ninja via the script bridge:  bnrun --timeout 900 tests/bn_golden.py
# Prepend settings to override, e.g. `LABEL = "after"; VIEW_NAME = "JVM Class dev3"`.
# Dumps the HLIL of a few reference classes to tests/golden/<LABEL>/<Class>.txt (git-ignored: sample code) and prints readability
# metrics over CLASS_DIR (first LIMIT classes): .d/.q accessor lines, $catch_ functions, functions
# whose signature has no type other than int32_t, analysis time. Exits non-zero when a class fails to
# open (jvm-94); the metrics are still printed and written first.
import os, re, time
import binaryninja as b

REPO = globals().get("REPO", "/Users/patrick/dev/binary-jvm")
LABEL = globals().get("LABEL", "baseline")
VIEW_NAME = globals().get("VIEW_NAME", "JVM Class")
CLASS_DIR = globals().get("CLASS_DIR", REPO + "/sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")
LIMIT = globals().get("LIMIT", 1000)
REFERENCE = globals().get("REFERENCE", ["com/is_teledata/cache/HashCache.class",
                                        "com/is_teledata/mdg/MDGAttributeDefinition.class",
                                        "com/is_teledata/property/PropLoader.class"])
FIELDREF = re.compile(r"&[\w/$<>]+\.[\w$<>]+")  # &pkg/Class.field (a field may be called d or q)
ACCESSOR = re.compile(r"[\w\]\)](:\d+)?(?<!sx)(?<!zx)\.[dq]\b(?!\()")  # var.d / var:4.q, not sx.q(...) or X.d(...) (same as bn_metrics.py)

vt = b.BinaryViewType[VIEW_NAME]
out_dir = os.path.join(REPO, "tests", "golden", LABEL)
os.makedirs(out_dir, exist_ok=True)

def hlil_lines(f):
    try:
        return [str(line) for line in f.hlil.root.lines] if f.hlil is not None else ["<no hlil>"]
    except Exception as e:
        return ["<hlil error %r>" % e]

paths = sorted(os.path.relpath(os.path.join(d, n), CLASS_DIR) for d, _, ns in os.walk(CLASS_DIR) for n in ns if n.endswith(".class"))[:LIMIT]
m = {"classes": 0, "functions": 0, "accessor_lines": 0, "accessor_classes": 0, "catch_funcs": 0, "int_only_sigs": 0, "seconds": 0.0}
times = []
failed = []
for rel in paths:
    t = time.time()
    raw = b.BinaryView.open(os.path.join(CLASS_DIR, rel))
    v = vt.create(raw) if raw else None
    if v is None:
        failed.append(rel); print("FAIL", rel); continue
    v.update_analysis_and_wait()
    times.append(time.time() - t)
    m["classes"] += 1
    acc = 0
    dump = []
    for f in sorted(v.functions, key=lambda f: f.start):
        m["functions"] += 1
        if "$catch_" in f.name:
            m["catch_funcs"] += 1
        sig = str(f.type)
        if all(str(t) == "int32_t" for t in [f.return_type] + [p.type for p in f.type.parameters]):
            m["int_only_sigs"] += 1
        lines = hlil_lines(f)
        acc += sum(1 for l in lines if ACCESSOR.search(FIELDREF.sub("", l)))
        if rel in REFERENCE:
            dump += ["", "// %s @ 0x%x" % (sig, f.start)] + lines
    m["accessor_lines"] += acc
    m["accessor_classes"] += acc > 0
    if dump:
        with open(os.path.join(out_dir, os.path.basename(rel).replace(".class", ".txt")), "w") as fh:
            fh.write("\n".join(dump) + "\n")
    v.file.close()
m["seconds"] = round(sum(times), 1)
if times:
    m["median_s"] = round(sorted(times)[len(times) // 2], 3)
print(m)
with open(os.path.join(out_dir, "metrics.txt"), "w") as fh:
    fh.write(repr(m) + "\n")
if failed:
    raise SystemExit("%d class(es) failed to open" % len(failed))
