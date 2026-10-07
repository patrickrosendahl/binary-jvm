# Run inside Binary Ninja via the script bridge (installed plugin, or after tests/bn_dev_load.py):
#   bnrun --timeout 900 tests/bn_class_view_dump.py
#   (prepend DEV_PKG = "jvm_devN" for a dev load, CLASSES = [...], OUT_DIR = "...")
# Writes the class view (`JVM\Show class`, classui.render_class) of each class to OUT_DIR/<pkg/Class>.java;
# tests/class_view_compare.py compares its declarations with Vineflower's.
import os, sys, time
import binaryninja as b

REPO = "/Users/patrick/dev/binary-jvm"
CLASS_DIR = globals().get("CLASS_DIR", REPO + "/sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")
OUT_DIR = globals().get("OUT_DIR", REPO + "/.scratch/vfcmp/cv")
CLASSES = globals().get("CLASSES", None)
if CLASSES is None:
    sys.path.insert(0, os.path.join(REPO, "tests"))
    import importlib, vineflower_compare
    vineflower_compare = importlib.reload(vineflower_compare)
    CLASSES = vineflower_compare.DEFAULT_CLASSES
PKG = globals().get("DEV_PKG") or (b._jvm_dev["pkg"].__name__ if hasattr(b, "_jvm_dev") else "binary-jvm")
classui = sys.modules[PKG + ".classui"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
print("package", PKG)
for rel in CLASSES:
    t = time.time()
    v = vt.create(b.BinaryView.open(os.path.join(CLASS_DIR, rel)))
    v.update_analysis_and_wait()
    skipped = [f for f in v.functions if f.analysis_skipped]
    if skipped:
        for f in skipped:
            f.analysis_skip_override = b.FunctionAnalysisSkipOverride.NeverSkipFunctionAnalysis
        v.update_analysis_and_wait()
    try:
        out = classui.render_class(v)
    except Exception as e:
        import traceback
        out = ["// render_class raised %r" % e] + ["// " + l for l in traceback.format_exc().splitlines()]
    path = os.path.join(OUT_DIR, rel[:-len(".class")] + ".java")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(out) + "\n")
    print("%-60s %.1fs" % (rel, time.time() - t))
    v.file.close()
