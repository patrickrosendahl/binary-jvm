# Run inside Binary Ninja via the script bridge (installed plugin, or after tests/bn_dev_load.py):
#   bnrun --parallel --timeout 600 tests/bn_pseudo_java_dump.py
#   (prepend DEV_PKG = "jvm_devN" for a dev load, CLASSES = [...], OUT_DIR = "...", RESUME = True)
# Writes the Pseudo Java of every method of each class to OUT_DIR/<pkg/Class>.pj, methods separated by
# "==== <name>" lines. tests/vineflower_compare.py compares these files with Vineflower's output.
# Each output gets a stamp in OUT_DIR/.stamps.json (tests/gate_state.py, jvm-105); RESUME skips a class whose
# output already carries the current stamp (jvm-106). A class with a method BN still skipped after
# NeverSkipFunctionAnalysis (load) gets no stamp, so RESUME redoes it (jvm-103).
import gc, os, sys, time
import binaryninja as b

REPO = "/Users/patrick/dev/binary-jvm"
CLASS_DIR = globals().get("CLASS_DIR", REPO + "/sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")
OUT_DIR = globals().get("OUT_DIR", REPO + "/.scratch/vfcmp/pj")
CLASSES = globals().get("CLASSES", None)
RESUME = globals().get("RESUME", False)
sys.path.insert(0, os.path.join(REPO, "tests"))
import importlib, gate_state
gate_state = importlib.reload(gate_state)  # the app's interpreter keeps modules between bnrun calls
if CLASSES is None:
    import vineflower_compare
    vineflower_compare = importlib.reload(vineflower_compare)
    CLASSES = vineflower_compare.DEFAULT_CLASSES
PKG = globals().get("DEV_PKG") or (b._jvm_dev["pkg"].__name__ if hasattr(b, "_jvm_dev") else "binary-jvm")
pj = sys.modules[PKG + ".pseudo_java"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
stamper = gate_state.Stamper("pj", sys.modules[PKG].__path__[0], PKG, OUT_DIR)
print("package", PKG, "language", pj.LANGUAGE_NAME)
resumed = 0
for rel in CLASSES:
    path = os.path.join(OUT_DIR, rel[:-len(".class")] + ".pj")
    if RESUME and stamper.current(rel, os.path.join(CLASS_DIR, rel), [path]):
        resumed += 1
        continue
    t = time.time()
    v = vt.create(b.BinaryView.open(os.path.join(CLASS_DIR, rel)))
    v.update_analysis_and_wait()
    skipped = [f for f in v.functions if f.analysis_skipped]
    if skipped:
        for f in skipped:
            f.analysis_skip_override = b.FunctionAnalysisSkipOverride.NeverSkipFunctionAnalysis
        v.update_analysis_and_wait()
    getattr(sys.modules[PKG + ".view"], "analyze_now", lambda v: None)(v)  # LVT names before rendering (jvm-97)
    out = []
    for f in sorted(v.functions, key=lambda f: f.start):
        out.append("==== %s" % f.name)
        try:
            out += pj.render_method(f)
        except Exception as e:
            out.append("// render raised %r" % e)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    skipped = [f.name for f in v.functions if f.analysis_skipped]
    if not skipped:  # skipped under load (jvm-103): no stamp, so RESUME redoes the class
        stamper.stamp(rel, os.path.join(CLASS_DIR, rel))
    print("%-60s %3d methods %.1fs%s" % (rel, len(v.functions), time.time() - t,
                                         "  SKIPPED " + " ".join(skipped) if skipped else ""))
    f = skipped = None  # no Function of this view left for a late finalizer (BN segfault in BNFreeFunction, jvm-110)
    gc.collect()  # cycles holding its Functions / HLIL go now, while the view is open
    v.file.close()
if resumed:
    print("RESUME: %d class(es) already current" % resumed)
