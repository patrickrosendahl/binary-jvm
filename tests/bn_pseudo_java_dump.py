# Run inside Binary Ninja via the script bridge (installed plugin, or after tests/bn_dev_load.py):
#   bnrun --parallel --timeout 600 tests/bn_pseudo_java_dump.py
#   (prepend DEV_PKG = "jvm_devN" for a dev load, CLASSES = [...], OUT_DIR = "...")
# Writes the Pseudo Java of every method of each class to OUT_DIR/<pkg/Class>.pj, methods separated by
# "==== <name>" lines. tests/vineflower_compare.py compares these files with Vineflower's output.
import os, sys, time
import binaryninja as b

REPO = "/Users/patrick/dev/binary-jvm"
CLASS_DIR = globals().get("CLASS_DIR", REPO + "/sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")
OUT_DIR = globals().get("OUT_DIR", REPO + "/.scratch/vfcmp/pj")
CLASSES = globals().get("CLASSES", None)
if CLASSES is None:
    sys.path.insert(0, os.path.join(REPO, "tests"))
    import importlib, vineflower_compare
    vineflower_compare = importlib.reload(vineflower_compare)
    CLASSES = vineflower_compare.DEFAULT_CLASSES
PKG = globals().get("DEV_PKG") or (b._jvm_dev["pkg"].__name__ if hasattr(b, "_jvm_dev") else "binary-jvm")
pj = sys.modules[PKG + ".pseudo_java"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
print("package", PKG, "language", pj.LANGUAGE_NAME)
for rel in CLASSES:
    t = time.time()
    v = vt.create(b.BinaryView.open(os.path.join(CLASS_DIR, rel)))
    v.update_analysis_and_wait()
    skipped = [f for f in v.functions if f.analysis_skipped]
    if skipped:
        for f in skipped:
            f.analysis_skip_override = b.FunctionAnalysisSkipOverride.NeverSkipFunctionAnalysis
        v.update_analysis_and_wait()
    out = []
    for f in sorted(v.functions, key=lambda f: f.start):
        out.append("==== %s" % f.name)
        try:
            out += pj.render_method(f)
        except Exception as e:
            out.append("// render raised %r" % e)
    path = os.path.join(OUT_DIR, rel[:-len(".class")] + ".pj")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("%-60s %3d methods %.1fs" % (rel, len(v.functions), time.time() - t))
    v.file.close()
