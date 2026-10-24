# Run inside Binary Ninja via the script bridge (installed plugin, or after tests/bn_dev_load.py):
#   bnrun --timeout 900 tests/bn_class_view_dump.py
#   (prepend DEV_PKG = "jvm_devN" for a dev load, CLASSES = [...], OUT_DIR = "...", RESUME = True)
# Writes the class view (`JVM\Show class`, classui.render_class) of each class to OUT_DIR/<pkg/Class>.java;
# tests/class_view_compare.py compares its declarations with Vineflower's. Stamps and RESUME as in
# bn_pseudo_java_dump.py (tests/gate_state.py, jvm-105/106).
import os, sys, time
import binaryninja as b

REPO = "/Users/patrick/dev/binary-jvm"
CLASS_DIR = globals().get("CLASS_DIR", REPO + "/sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")
OUT_DIR = globals().get("OUT_DIR", REPO + "/.scratch/vfcmp/cv")
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
classui = sys.modules[PKG + ".classui"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
stamper = gate_state.Stamper("cv", sys.modules[PKG].__path__[0], PKG, OUT_DIR)
print("package", PKG)
resumed = 0
for rel in CLASSES:
    path = os.path.join(OUT_DIR, rel[:-len(".class")] + ".java")
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
    try:
        out = classui.render_class(v)
    except Exception as e:
        import traceback
        out = ["// render_class raised %r" % e] + ["// " + l for l in traceback.format_exc().splitlines()]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    stamper.stamp(rel, os.path.join(CLASS_DIR, rel))
    print("%-60s %.1fs" % (rel, time.time() - t))
    v.file.close()
if resumed:
    print("RESUME: %d class(es) already current" % resumed)
