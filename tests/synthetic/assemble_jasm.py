"""Assemble the subset of Krakatau-style jasm used for rare opcodes (jvm-37).

No JDK. Supports .version/.class/.super/.method/.code, labels, no-operand opcodes, every
16-bit branch (ifeq, goto, jsr, ...) and the 32-bit goto_w/jsr_w, ret, iinc, and wide
(including wide iinc / wide ret). Branch offsets are
relative to the opcode byte, as in the JVM spec. The emitted code is decoded back with opcodes.py
before the class file is written.

    python3 tests/synthetic/assemble_jasm.py tests/synthetic/rare/RareOps.jasm -d tests/synthetic/classes
"""
import argparse
import os
import struct
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import opcodes

NAME_TO_OP = {name: i for i, name in enumerate(opcodes.InstructionNames) if name}

def branch_wide(mnem):
    """False for a 16-bit branch, True for a 32-bit one, None if mnem is not a branch."""
    op = NAME_TO_OP.get(mnem)
    if op is None:
        return None
    fmt = opcodes.InstructionFormat[op]
    if fmt == opcodes.TYPE_2BRANCH:
        return False
    if fmt == opcodes.TYPE_4BRANCH:
        return True
    return None
CLASS_FLAGS = {"public": 0x0001, "final": 0x0010, "super": 0x0020, "interface": 0x0200, "abstract": 0x0400}
METHOD_FLAGS = {"public": 0x0001, "private": 0x0002, "protected": 0x0004, "static": 0x0008, "final": 0x0010,
                "synchronized": 0x0020, "bridge": 0x0040, "varargs": 0x0080, "native": 0x0100,
                "abstract": 0x0400, "strict": 0x0800, "synthetic": 0x1000}


class Pool:
    def __init__(self):
        self.blobs = [None]
        self.keys = {}

    def _add(self, key, blob):
        if key in self.keys:
            return self.keys[key]
        self.keys[key] = len(self.blobs)
        self.blobs.append(blob)
        return self.keys[key]

    def utf8(self, text):
        raw = text.encode("utf-8")
        if len(raw) > 0xFFFF:
            raise ValueError("UTF-8 too long: %s" % text)
        return self._add(("utf", text), b"\x01" + struct.pack(">H", len(raw)) + raw)

    def cls(self, name):
        return self._add(("class", name), b"\x07" + struct.pack(">H", self.utf8(name)))

    def bytes(self):
        return b"".join(self.blobs[1:])

    def count(self):
        return len(self.blobs)


def _strip(line):
    for sep in ("#", ";"):
        if sep in line:
            line = line.split(sep, 1)[0]
    return line.split()


def _u2(n, what):
    if not 0 <= n <= 0xFFFF:
        raise ValueError("%s out of range: %s" % (what, n))
    return struct.pack(">H", n)


def insn_len(mnem, args):
    wide = branch_wide(mnem)
    if wide is not None:
        return 5 if wide else 3
    if mnem == "wide":
        return 6 if args[0] == "iinc" else 4
    if mnem == "ret":
        return 2
    if mnem == "iinc":
        return 3
    op = NAME_TO_OP.get(mnem)
    if op is not None and opcodes.InstructionFormat[op] == opcodes.TYPE_NONE:
        return 1
    raise ValueError("unknown instruction %s" % mnem)


def emit_insn(mnem, args, pc, labels):
    wide = branch_wide(mnem)
    if wide is not None:
        if args[0] not in labels:
            raise ValueError("unknown label %s" % args[0])
        off = labels[args[0]] - pc
        op = NAME_TO_OP[mnem]
        if wide:
            return bytes([op]) + struct.pack(">i", off)
        if not -32768 <= off <= 32767:
            raise ValueError("%s offset %d does not fit; use %s_w" % (mnem, off, mnem))
        return bytes([op]) + struct.pack(">h", off)
    if mnem == "wide":
        sub = args[0]
        if sub == "iinc":
            return bytes([0xc4, 0x84]) + struct.pack(">Hh", int(args[1]), int(args[2]))
        subop = NAME_TO_OP.get(sub)
        if subop not in opcodes.WIDE_OPCODES:
            raise ValueError("wide %s is not a wide opcode" % sub)
        return bytes([0xc4, subop]) + _u2(int(args[1]), "wide index")
    if mnem == "ret":
        idx = int(args[0])
        if not 0 <= idx <= 255:
            raise ValueError("ret %d needs wide ret" % idx)
        return bytes([0xa9, idx])
    if mnem == "iinc":
        return bytes([0x84, int(args[0])]) + struct.pack(">b", int(args[1]))
    return bytes([NAME_TO_OP[mnem]])


def assemble_code(items):
    """items: ('label', name) or ('insn', mnem, args). Returns code bytes."""
    labels = {}
    pc = 0
    placed = []
    for item in items:
        if item[0] == "label":
            if item[1] in labels:
                raise ValueError("duplicate label %s" % item[1])
            labels[item[1]] = pc
        else:
            placed.append((pc, item[1], item[2]))
            pc += insn_len(item[1], item[2])
    out = b""
    for pc, mnem, args in placed:
        blob = emit_insn(mnem, args, pc, labels)
        if len(blob) != insn_len(mnem, args):
            raise ValueError("length mismatch for %s" % mnem)
        out += blob
    off = 0
    for _pc, mnem, args in placed:
        name, _operand, length, value = opcodes.decode_instruction(out[off:], off)
        if name is None or off + length > len(out):
            raise ValueError("emitted %s does not decode" % mnem)
        if branch_wide(mnem) is not None:
            if name != mnem or value != labels[args[0]]:
                raise ValueError("%s decoded as %s -> %s, label at %s" % (mnem, name, value, labels[args[0]]))
        elif mnem == "wide":
            if name != "wide" or opcodes.InstructionNames[value[0]] != args[0]:
                raise ValueError("wide %s decoded as %s %s" % (args[0], name, value))
        elif name != mnem:
            raise ValueError("%s decoded as %s" % (mnem, name))
        off += length
    if off != len(out):
        raise ValueError("decode did not tile the code")
    return out


def _flags(tokens, table, what):
    value = 0
    i = 0
    while i < len(tokens) and tokens[i] in table:
        value |= table[tokens[i]]
        i += 1
    if i == len(tokens):
        raise ValueError("%s is missing a name" % what)
    return value, tokens[i], tokens[i + 1:]


def _method_tokens(tokens):
    body = []
    for tok in tokens:
        if tok == ":":
            continue
        if ":" in tok:
            name, desc = tok.split(":", 1)
            if name:
                body.append(name)
            if desc:
                body.append(desc)
        else:
            body.append(tok)
    return body


def parse_jasm(text, origin):
    major, minor = 49, 0
    class_flags, class_name, super_name = 0x0021, None, None
    methods = []
    method = None
    in_code = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        tok = _strip(raw)
        if not tok:
            continue
        try:
            if tok[0] == ".version":
                major, minor = int(tok[1]), int(tok[2])
            elif tok[0] == ".class":
                class_flags, class_name, _rest = _flags(tok[1:], CLASS_FLAGS, ".class")
            elif tok[0] == ".super":
                super_name = tok[1]
            elif tok[0] == ".method":
                if method is not None:
                    raise ValueError("method %s is not closed" % method["name"])
                flags, name, rest = _flags(_method_tokens(tok[1:]), METHOD_FLAGS, ".method")
                if len(rest) != 1 or not rest[0].startswith("("):
                    raise ValueError("method needs a descriptor, got %r" % rest)
                method = {"flags": flags, "name": name, "desc": rest[0], "stack": None, "locals": None, "items": []}
                in_code = False
            elif tok[0] == ".code":
                if method is None or in_code:
                    raise ValueError(".code outside a method")
                if tok[1:5] != ["stack", tok[2], "locals", tok[4]] or len(tok) != 5:
                    raise ValueError(".code stack <n> locals <n>")
                method["stack"] = int(tok[2])
                method["locals"] = int(tok[4])
                in_code = True
            elif tok[:2] == [".end", "code"]:
                in_code = False
            elif tok[:2] == [".end", "method"]:
                if method is None or method["stack"] is None:
                    raise ValueError(".end method without a .code")
                methods.append(method)
                method = None
            elif tok[:2] == [".end", "class"]:
                if method is not None:
                    raise ValueError(".end class inside a method")
                break
            elif tok[0].endswith(":") and len(tok) == 1:
                if not in_code:
                    raise ValueError("label outside .code")
                method["items"].append(("label", tok[0][:-1]))
            else:
                if not in_code:
                    raise ValueError("instruction outside .code: %s" % tok[0])
                method["items"].append(("insn", tok[0], tok[1:]))
                insn_len(tok[0], tok[1:])
        except Exception as e:
            raise ValueError("%s:%d: %s" % (origin, lineno, e)) from e
    if method is not None:
        raise ValueError("%s: method %s is not closed" % (origin, method["name"]))
    if not class_name or not super_name:
        raise ValueError("%s: .class and .super are required" % origin)
    return {"major": major, "minor": minor, "flags": class_flags, "name": class_name,
            "super": super_name, "methods": methods}


def build_class(cls):
    pool = Pool()
    this = pool.cls(cls["name"])
    super_ = pool.cls(cls["super"])
    code_name = pool.utf8("Code")
    method_blobs = []
    for method in cls["methods"]:
        code = assemble_code(method["items"])
        body = (_u2(method["stack"], "max_stack") + _u2(method["locals"], "max_locals")
                + struct.pack(">I", len(code)) + code + struct.pack(">HH", 0, 0))
        attr = _u2(code_name, "Code") + struct.pack(">I", len(body)) + body
        method_blobs.append(
            _u2(method["flags"], "access") + _u2(pool.utf8(method["name"]), "name")
            + _u2(pool.utf8(method["desc"]), "descriptor") + struct.pack(">H", 1) + attr)
    return (b"\xca\xfe\xba\xbe" + _u2(cls["minor"], "minor") + _u2(cls["major"], "major")
            + _u2(pool.count(), "pool") + pool.bytes()
            + _u2(cls["flags"], "class flags") + _u2(this, "this") + _u2(super_, "super")
            + struct.pack(">HH", 0, 0) + _u2(len(method_blobs), "methods") + b"".join(method_blobs)
            + struct.pack(">H", 0))


def assemble_file(path, dest):
    cls = parse_jasm(open(path, "r", encoding="utf-8").read(), path)
    raw = build_class(cls)
    out = os.path.join(dest, cls["name"] + ".class")
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(raw)
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sources", nargs="+")
    ap.add_argument("-d", "--dest", required=True)
    args = ap.parse_args(argv)
    for path in args.sources:
        print(assemble_file(path, args.dest))


if __name__ == "__main__":
    main(sys.argv[1:])
