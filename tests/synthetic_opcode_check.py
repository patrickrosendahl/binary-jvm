"""Lift the rare-opcode class and require the opcodes the sample never hits (jvm-37).

nop, swap, goto_w and jsr_w do not occur under sample/. wide's modified instructions are sparse
there too, so tests/synthetic/rare/RareOps.jasm carries each wide form (loads, stores, iinc, ret).
The committed .class must be exactly what tests/synthetic/assemble_jasm.py emits. Stack effect,
tiling and branch targets go through the same checker as tests/offline_lift_check.py.

usage: python3 tests/synthetic_opcode_check.py
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import offline_lift_check as olc
from binary_jvm import classfile, opcodes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JASM = os.path.join(ROOT, "tests", "synthetic", "rare", "RareOps.jasm")
CLASS = olc.RARE_CLASS
REQUIRED = ("nop", "swap", "goto_w", "jsr_w")
WIDE = ("iload", "lload", "fload", "dload", "aload", "istore", "lstore", "fstore", "dstore", "astore", "iinc", "ret")


def assemble_bytes():
    path = os.path.join(ROOT, "tests", "synthetic", "assemble_jasm.py")
    spec = importlib.util.spec_from_file_location("assemble_jasm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = mod.parse_jasm(open(JASM, encoding="utf-8").read(), JASM)
    return mod.build_class(cls)


def wide_forms(raw):
    reader = classfile.JVMClassReader(olc.Dummy(), olc.Data(raw))
    reader.charType = olc.Dummy()
    cls = classfile.JVMClassStructure(reader)
    assert reader.index() == len(raw), "parser stopped at %d of %d" % (reader.index(), len(raw))
    forms = set()
    for method in cls.methods:
        code = method.code_attribute.attribute
        blob = raw[code.start_address:code.end_address]
        off = 0
        while off < len(blob):
            name, _operand, length, value = opcodes.decode_instruction(blob[off:], off)
            assert name is not None, "%s +%x" % (method.name, off)
            if name == "wide":
                forms.add(opcodes.InstructionNames[value[0]])
            off += length
        assert off == len(blob), method.name
    return forms


def main():
    fresh = assemble_bytes()
    committed = open(CLASS, "rb").read()
    assert fresh == committed, "RareOps.class does not match %s; re-run tests/synthetic/assemble_jasm.py" % JASM
    forms = wide_forms(committed)
    missing_wide = [name for name in WIDE if name not in forms]
    assert not missing_wide, "wide forms missing: %s" % " ".join(missing_wide)
    stats = olc.new_stats()
    olc.check_class(CLASS, committed, stats)
    for key in olc.KEYS:
        assert not stats[key], "%s: %s" % (key, stats[key][:8])
    missing = [name for name in REQUIRED if not stats["count"][name]]
    assert not missing, "opcodes not lifted: %s" % " ".join(missing)
    assert stats["count"]["wide"] >= len(WIDE), stats["count"]["wide"]
    print("ok %s" % " ".join("%s=%d" % (name, stats["count"][name]) for name in REQUIRED + ("wide",)))


if __name__ == "__main__":
    main()
