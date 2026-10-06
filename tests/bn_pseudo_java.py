# Run inside Binary Ninja via the script bridge, after tests/bn_dev_load.py:
#   bnrun --parallel --timeout 300 tests/bn_pseudo_java.py   (prepend DEV_PKG = "jvm_devN", CLASSES = [...],
#   FUNCS = [...], HLIL = False)
# Creates the dev JVM view for a few classes and prints every method through the dev Pseudo-Java language
# (pseudo_java.render_method), optionally next to the plain HLIL.
import os, sys, time
import binaryninja as b

CLASS_DIR = globals().get("CLASS_DIR", "/Users/patrick/dev/binary-jvm/sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")
CLASSES = globals().get("CLASSES", ["com/is_teledata/cache/HashCache.class"])
FUNCS = globals().get("FUNCS", None)
HLIL = globals().get("HLIL", True)
# DEV_PKG = "jvm_devN" picks a specific dev load (other sessions may have loaded newer ones since)
PKG = globals().get("DEV_PKG") or b._jvm_dev["pkg"].__name__
pj = sys.modules[PKG + ".pseudo_java"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
print("language:", pj.LANGUAGE_NAME, "registered:", pj.LANGUAGE_NAME in b.LanguageRepresentationFunctionType)
for rel in CLASSES:
    t = time.time()
    v = vt.create(b.BinaryView.open(os.path.join(CLASS_DIR, rel)))
    v.update_analysis_and_wait()
    skipped = [f for f in v.functions if f.analysis_skipped]
    if skipped:  # analysis time/size limits (busy app): force these
        print("forcing analysis of", [(f.name, f.analysis_skip_reason.name) for f in skipped])
        for f in skipped:
            f.analysis_skip_override = b.FunctionAnalysisSkipOverride.NeverSkipFunctionAnalysis
        v.update_analysis_and_wait()
    print("#### %s (%.1fs)" % (rel, time.time() - t))
    for f in sorted(v.functions, key=lambda f: f.start):
        if FUNCS and f.name not in FUNCS:
            continue
        print("=" * 20, f.name)
        if HLIL:
            for line in f.hlil.root.lines:
                print("   HLIL |", line)
        try:
            for line in pj.render_method(f):
                print("   JAVA |", line)
        except Exception as e:
            import traceback; traceback.print_exc()
    v.file.close()
