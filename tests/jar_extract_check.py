"""Whole JAR unpacks .class entries beside the jar and refuses paths that leave that directory."""
import importlib.util, os, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("jarload", os.path.join(ROOT, "jarload.py"))
jarload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jarload)

tmp = tempfile.mkdtemp()
jar_path = os.path.join(tmp, "demo.jar")
with zipfile.ZipFile(jar_path, "w") as zf:
    zf.writestr("com/example/A.class", b"\xca\xfe\xba\xbeA")
    zf.writestr("com/example/B.class", b"\xca\xfe\xba\xbeB")
    zf.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")
    zf.writestr("../escape.class", b"nope")
paths = jarload.extract_classes(jar_path)
assert jarload.jar_dest(jar_path) == os.path.abspath(os.path.join(tmp, "demo"))
assert paths == [
    os.path.join(tmp, "demo", "com", "example", "A.class"),
    os.path.join(tmp, "demo", "com", "example", "B.class"),
]
assert open(paths[0], "rb").read() == b"\xca\xfe\xba\xbeA"
assert not os.path.exists(os.path.join(tmp, "escape.class"))
listed = jarload.classes_in(jarload.jar_dest(jar_path))
assert [name for name, _path in listed] == ["com/example/A", "com/example/B"]
print("ok")
