"""The newer class-file attributes parse, and the reader finishes at the end of the file (jvm-24).

Uses the committed classes under tests/synthetic/attrs/classes (built by tests/synthetic/attrs/build.sh).
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import offline_lift_check as olc
from binary_jvm import classfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "synthetic", "attrs", "classes")


def load(rel):
    raw = open(os.path.join(ROOT, rel), "rb").read()
    reader = classfile.JVMClassReader(olc.Dummy(), olc.Data(raw))
    reader.charType = olc.Dummy()
    cls = classfile.JVMClassStructure(reader)
    assert reader.index() == len(raw), "%s: parser stopped at %d of %d" % (rel, reader.index(), len(raw))
    return cls


def attr(owner, kind):
    return classfile.find_attribute(owner, kind)


def main():
    host = load("jvmtest/Attrs.class")
    nest = attr(host.attributes, "NestMembers")
    assert nest and len(nest.classes) >= 2, nest
    names = {classfile.pool_text(host.classReader, i) for i in nest.classes}
    assert "jvmtest/Attrs$Inner" in names and "jvmtest/Attrs$Point" in names, names

    inner = load("jvmtest/Attrs$Inner.class")
    host_attr = attr(inner.attributes, "NestHost")
    assert classfile.pool_text(inner.classReader, host_attr.host_class_index) == "jvmtest/Attrs"

    point = load("jvmtest/Attrs$Point.class")
    rec = attr(point.attributes, "Record")
    comps = [(classfile.pool_text(point.classReader, n), classfile.pool_text(point.classReader, d))
             for n, d, _ in rec.components]
    assert comps == [("x", "I"), ("y", "I")], comps

    shape = load("jvmtest/Shape.class")
    permitted = attr(shape.attributes, "PermittedSubclasses")
    assert [classfile.pool_text(shape.classReader, i) for i in permitted.classes] == ["jvmtest/Circle"]

    by_name = {m.name: m for m in host.methods}
    frames = attr(by_name["abs"].code_attribute.attribute.attributes, "StackMapTable")
    assert frames and frames.number_of_entries >= 1, frames
    types = attr(by_name["id"].code_attribute.attribute.attributes, "LocalVariableTypeTable")
    assert types and types.local_variable_type_table_length >= 1
    params = attr(by_name["named"].attributes, "MethodParameters")
    assert [classfile.pool_text(host.classReader, i) for i, _ in params.parameters] == ["label"]

    # a Java 5 sample class still parses to the end (no StackMapTable; unknown attrs skipped by length)
    sample = os.path.join(olc.ROOT, "sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg/"
                          "com/is_teledata/net/ErrorHandler.class")
    raw = open(sample, "rb").read()
    reader = classfile.JVMClassReader(olc.Dummy(), olc.Data(raw))
    reader.charType = olc.Dummy()
    classfile.JVMClassStructure(reader)
    assert reader.index() == len(raw)
    print("ok")


if __name__ == "__main__":
    main()
