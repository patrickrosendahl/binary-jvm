"""Whole JAR unpacks .class entries beside the jar and refuses paths that leave that directory."""
import importlib.util, io, os, sys, tempfile, zipfile
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
scan = jarload.scan_jar(jar_path)
assert scan.classes == ["com/example/A.class", "com/example/B.class"]
assert scan.nested == []
assert scan.unsafe == ["../escape.class"]
assert scan.other == ["META-INF/MANIFEST.MF"]
assert jarload.main_class_of(scan.attributes) is None
assert not os.path.exists(os.path.join(tmp, "demo", "META-INF", "MANIFEST.MF"))
assert jarload.skipped_summary(scan.other, scan.unsafe) == (
    "skipped 1 non-class resource, 1 entry outside the unpack directory")
assert jarload.skipped_summary([], []) is None

# manifest: continuations, last Main-Class wins, per-entry sections do not override, CRLF
attrs = jarload.manifest_main_attributes(
    b"Manifest-Version: 1.0\r\nMain-Class: com.example.Long\r\n Name\r\n\r\n"
    b"Name: com/example/A.class\r\nMain-Class: com.example.Other\r\n")
assert jarload.main_class_of(attrs) == "com/example/LongName"
attrs = jarload.manifest_main_attributes(b"Main-Class: com.example.A\nMain-Class: com.example.B\n")
assert jarload.main_class_of(attrs) == "com/example/B"
attrs = jarload.manifest_main_attributes(b"main-class: com.example.A.class\n")
assert jarload.main_class_of(attrs) == "com/example/A"
assert jarload.main_class_binary("  ") is None
assert jarload.main_class_binary("../Evil") is None

# nested JARs: foo.jar!/lib/bar.jar -> foo/lib/bar/, resources and a deeper jar stay packed
inner_buf = io.BytesIO()
with zipfile.ZipFile(inner_buf, "w") as inner:
    inner.writestr("com/example/N.class", b"\xca\xfe\xba\xbeN")
    inner.writestr("readme.txt", b"hi")
    inner.writestr("../escape.class", b"nope")
    inner.writestr("lib/deeper.jar", b"not-a-zip")
outer_path = os.path.join(tmp, "outer.jar")
with zipfile.ZipFile(outer_path, "w") as outer:
    outer.writestr("com/example/M.class", b"\xca\xfe\xba\xbeM")
    outer.writestr("lib/bar.jar", inner_buf.getvalue())
    outer.writestr("lib/Bar.JAR", inner_buf.getvalue())  # case of the suffix
    outer.writestr("notes.txt", b"x")
    outer.writestr("../evil.jar", b"nope")
    outer.writestr("bad.jar", b"this is not a zip")
    manifest = (b"Manifest-Version: 1.0\n"
                b"Main-Class: com.example.VeryLongNameThatKeepsGoing\n"
                b" AndGoing\n")
    outer.writestr("META-INF/MANIFEST.MF", manifest)
outer_scan = jarload.scan_jar(outer_path)
assert outer_scan.classes == ["com/example/M.class"]
assert outer_scan.nested == ["bad.jar", "lib/Bar.JAR", "lib/bar.jar"]
assert outer_scan.unsafe == ["../evil.jar"]
assert "notes.txt" in outer_scan.other and "META-INF/MANIFEST.MF" in outer_scan.other
assert jarload.main_class_of(outer_scan.attributes) == "com/example/VeryLongNameThatKeepsGoingAndGoing"
assert jarload.nested_unpack_dir(outer_path, "lib/bar.jar") == os.path.join(tmp, "outer", "lib", "bar")
assert jarload.nested_unpack_dir(outer_path, "lib/Bar.JAR") == os.path.join(tmp, "outer", "lib", "Bar")
assert jarload.nested_unpack_dir(outer_path, "../evil.jar") is None
assert jarload.nested_unpack_dir("/tmp/foo.jar", "lib\\bar.jar") == "/tmp/foo/lib/bar"
written, further, unsafe = jarload.unpack_nested_jar(outer_path, "lib/bar.jar")
assert written == [os.path.join(tmp, "outer", "lib", "bar", "com", "example", "N.class")]
assert open(written[0], "rb").read() == b"\xca\xfe\xba\xbeN"
assert further == ["lib/deeper.jar"]
assert unsafe == ["../escape.class"]
assert not os.path.exists(os.path.join(tmp, "outer", "lib", "bar", "readme.txt"))
assert not os.path.exists(os.path.join(tmp, "escape.class"))
assert not os.path.exists(os.path.join(tmp, "outer", "lib", "escape.class"))
assert not os.path.exists(os.path.join(tmp, "outer", "lib", "bar.jar"))
try:
    jarload.unpack_nested_jar(outer_path, "bad.jar")
    raise AssertionError("bad.jar should not unpack")
except zipfile.BadZipFile:
    pass
try:
    jarload.unpack_nested_jar(outer_path, "../evil.jar")
    raise AssertionError("evil.jar should not unpack")
except ValueError:
    pass
jarload.extract_classes(outer_path)
assert os.path.exists(os.path.join(tmp, "outer", "com", "example", "M.class"))
assert not os.path.exists(os.path.join(tmp, "outer", "notes.txt"))
found = jarload.classes_in(jarload.jar_dest(outer_path))
assert jarload.find_main_class(found, "com/example/M") == "com/example/M"
assert jarload.find_main_class(found, "com/example/N") == "lib/bar/com/example/N"
ambiguous = [("lib/a/com/example/N", "/a"), ("lib/b/com/example/N", "/b")]
assert jarload.find_main_class(ambiguous, "com/example/N") is None
assert jarload.find_main_class(found, "com/example/Missing") is None
assert jarload.main_class_note("com/example/M", "com/example/M") == "Main-Class: com.example.M"
assert "not among these classes" in jarload.main_class_note("com/example/Missing", None)
assert jarload.nested_left_message(["lib/a.jar"]) == "left nested JARs packed: lib/a.jar"
assert jarload.manifest_main_class(jarload.jar_dest(outer_path)) == (
    "com/example/VeryLongNameThatKeepsGoingAndGoing")
assert jarload.manifest_main_class(os.path.join(tmp, "no-such-dir")) is None
print("ok")
