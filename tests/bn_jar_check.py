# Run in Binary Ninja: bn --timeout 300 tests/bn_exec.py tests/bn_jar_check.py [SHOW=True]
# Whole JAR with a nested jar (tests/synthetic/jar/app.jar, built by its build.sh): unpacked into a temp dir the way
# Load Whole JAR does (app/, nested lib/helper.jar -> app/lib/helper/), a class of the outer jar must see the classes
# of the nested one (jvm-120): app.Other's varargs call into lib.Helper prints with plain arguments and its override
# of lib.Base.name() gets @Override in the class view. Prints PASS / FAIL and exits non-zero on a failure.
import os, shutil, sys, tempfile
import binaryninja as b

REPO = "/Users/patrick/dev/binary-jvm"
PKG = "binary-jvm"
SHOW = globals().get("SHOW", False)
jarload = sys.modules[PKG + ".jarload"]
pj = sys.modules[PKG + ".pseudo_java"]
classui = sys.modules[PKG + ".classui"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]

tmp = tempfile.mkdtemp()
jar = os.path.join(tmp, "app.jar")
shutil.copy(os.path.join(REPO, "tests/synthetic/jar/app.jar"), jar)
jarload.extract_classes(jar)
jarload.unpack_nested_jar(jar, "lib/helper.jar")
v = vt.create(b.BinaryView.open(os.path.join(tmp, "app", "app", "Other.class")))
v.update_analysis_and_wait()
sys.modules[PKG + ".view"].analyze_now(v)
failed = 0
f = next(f for f in v.functions if f.name == "name")
text = "\n".join(pj.render_method(f))
cls = "\n".join(classui.render_class(v))
for label, ok in (("varargs call into the nested jar", 'Helper.join("other", "x")' in text and "new String" not in text),
                  ("@Override of a nested jar's method", "@Override" in cls)):
    failed += not ok
    print("%s %s" % ("PASS" if ok else "FAIL", label))
if failed or SHOW:
    print(text)
    print(cls)
v.file.close()
shutil.rmtree(tmp)
sys.exit(1 if failed else 0)
