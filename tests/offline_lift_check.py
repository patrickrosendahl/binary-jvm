"""Offline sanity check for the plugin's decoder + lifter (no Binary Ninja licence needed).

Stubs out the `binaryninja` module, loads the plugin package, parses every .class in the given
.class/.jar files, linearly decodes every method and lifts each instruction into a recording mock IL.
Checks that:
  * every instruction decodes and the decoded lengths tile the method's code exactly,
  * no instruction lifts to `unimplemented`,
  * the net operand-stack effect of the lifted IL (bytes pushed - popped) matches an independent
    JVM stack-effect table (in 4-byte slots).
Runs in parallel over chunks of classes.

usage: python3 tests/offline_lift_check.py [--all | file.jar|file.class ...]
       no arguments: quick check on sample/.../lib/mdg.jar;  --all: every .jar/.class under sample/
"""
import collections, importlib.util, multiprocessing, os, sys, time, types, zipfile

# ---- binaryninja stub -------------------------------------------------------------------------
class Dummy:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name): return Dummy()
    def __call__(self, *a, **k): return Dummy()
    def __eq__(self, other): return True
    def __hash__(self): return 0
    def __or__(self, other): return self
    __ror__ = __or__

class DummyMeta(type):
    def __getitem__(cls, key): return Dummy()
    def __getattr__(cls, name): return Dummy()

class DummyClass(metaclass=DummyMeta):
    def __init__(self, *a, **k): pass

def _stub_attr(name):
    if name == "LLIL_TEMP":
        return lambda n: 0x80000000 | n
    return type(name, (DummyClass,), {})  # every other binaryninja name: an inert class

bn = types.ModuleType("binaryninja")
bn.__getattr__ = _stub_attr
sys.modules["binaryninja"] = bn

# the repo root is the plugin package; load it as "binary_jvm"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_spec = importlib.util.spec_from_file_location("binary_jvm", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["binary_jvm"] = _pkg
_spec.loader.exec_module(_pkg)
from binary_jvm import opcodes, lifter, classfile

# ---- recording IL -------------------------------------------------------------------------------
class MockIL:
    arch = None
    def __init__(self):
        self.delta = 0
        self.unimplemented_used = False
    def append(self, expr): pass
    def mark_label(self, label): pass
    def get_label_for_address(self, arch, addr): return None
    def push(self, size, expr): self.delta += size; return ("push", size)
    def pop(self, size): self.delta -= size; return ("pop", size)
    def call_stack_adjust(self, dest, adjust): self.delta -= adjust; return ("call",)
    def unimplemented(self): self.unimplemented_used = True; return ("unimpl",)
    def intrinsic(self, outputs, name, params):
        assert name in lifter.INTRINSICS, name
        return ("intrinsic", name)
    def __getattr__(self, name):
        return lambda *a, **k: (name,)

class Data:
    def __init__(self, b): self.b = b
    def read(self, off, n): return self.b[off:off+n]
    def __len__(self): return len(self.b)

# ---- independent stack-effect table (slots) ------------------------------------------------------
EFFECT = {}
def eff(names, n):
    for x in names.split(): EFFECT[x] = n
eff("nop swap ineg lneg fneg dneg iinc i2f l2d f2i d2l i2b i2c i2s goto goto_w ret newarray anewarray arraylength checkcast instanceof", 0)
eff("aconst_null iconst_m1 iconst_0 iconst_1 iconst_2 iconst_3 iconst_4 iconst_5 fconst_0 fconst_1 fconst_2 bipush sipush ldc ldc_w dup dup_x1 dup_x2 i2l i2d f2l f2d new", 1)
eff("lconst_0 lconst_1 dconst_0 dconst_1 ldc2_w dup2 dup2_x1 dup2_x2", 2)
for k, n in (("i", 1), ("f", 1), ("a", 1), ("l", 2), ("d", 2)):
    eff(" ".join([k+"load"] + ["%sload_%d" % (k, i) for i in range(4)]), n)
    eff(" ".join([k+"store"] + ["%sstore_%d" % (k, i) for i in range(4)]), -n)
eff("iaload faload aaload baload caload saload", -1)
eff("laload daload", 0)
eff("iastore fastore aastore bastore castore sastore", -3)
eff("lastore dastore", -4)
eff("pop", -1); eff("pop2", -2)
eff("iadd isub imul idiv irem ishl ishr iushr iand ior ixor fadd fsub fmul fdiv frem", -1)
eff("ladd lsub lmul ldiv lrem land lor lxor dadd dsub dmul ddiv drem", -2)
eff("lshl lshr lushr", -1)
eff("l2i l2f d2i d2f fcmpl fcmpg", -1)
eff("lcmp dcmpl dcmpg", -3)
eff("ifeq ifne iflt ifge ifgt ifle ifnull ifnonnull tableswitch lookupswitch ireturn freturn areturn athrow monitorenter monitorexit", -1)
eff("if_icmpeq if_icmpne if_icmplt if_icmpge if_icmpgt if_icmple if_acmpeq if_acmpne lreturn dreturn", -2)
eff("return", 0)

def expected(reader, name, value):
    if name in EFFECT: return EFFECT[name]
    if name == "wide": return expected(reader, opcodes.InstructionNames[value[0]], value[1])
    if name == "jsr" or name == "jsr_w": return 0  # pushed return address is popped by the "call"
    if name == "multianewarray": return 1 - value[1]
    index = value[0] if isinstance(value, tuple) else value
    desc = reader.memberDescriptor(index)
    if name in ("getstatic", "putstatic", "getfield", "putfield"):
        s = 2 if desc[0] in "JD" else 1
        return {"getstatic": s, "putstatic": -s, "getfield": s-1, "putfield": -s-1}[name]
    args, ret = opcodes.parse_method_descriptor(desc)
    n = -sum(2 if a in "JD" else 1 for a in args)
    if name not in ("invokestatic", "invokedynamic"): n -= 1
    return n + (0 if ret == "V" else 2 if ret in "JD" else 1)

# ---- driver -------------------------------------------------------------------------------------
KEYS = ("decode_fail", "lift_error", "unimplemented", "stack_mismatch", "tiling")

def check_class(label, raw, stats):
    reader = classfile.JVMClassReader(Dummy(), Data(raw))
    reader.charType = Dummy()
    cls = classfile.JVMClassStructure(reader)
    reader.classStruct = cls
    for m in cls.methods:
        if m.code_attribute is None: continue
        code = m.code_attribute.attribute
        base = 0x1000000 + 0x100000*m.index
        code_bytes = raw[code.start_address:code.end_address]
        off = 0
        while off < len(code_bytes):
            addr = base + off
            name, operand, length, value = opcodes.decode_instruction(code_bytes[off:], addr)
            if name is None:
                stats["decode_fail"].append("%s m%d +%x" % (label, m.index, off)); break
            il = MockIL()
            try:
                lifter.InstructionIL[name](il, value, lifter.LiftContext(addr, length, reader))
            except Exception as e:
                stats["lift_error"].append("%s m%d +%x %s: %r" % (label, m.index, off, name, e))
                off += length; continue
            stats["count"][name] += 1
            if il.unimplemented_used: stats["unimplemented"].append("%s %s" % (label, name))
            exp = expected(reader, name, value) * 4
            if il.delta != exp:
                stats["stack_mismatch"].append("%s m%d +%x %s: lifted %d, expected %d" % (label, m.index, off, name, il.delta, exp))
            off += length
        if off != len(code_bytes):
            stats["tiling"].append("%s m%d" % (label, m.index))

def new_stats():
    stats = {"count": collections.Counter(), "classes": 0}
    for k in KEYS: stats[k] = []
    return stats

def check_chunk(chunk):
    stats = new_stats()
    for label, raw in chunk:
        stats["classes"] += 1
        try:
            check_class(label, raw, stats)
        except Exception as e:
            stats["lift_error"].append("%s: parse failed %r" % (label, e))
    return stats

def chunks(paths, size=200):
    chunk = []
    for path in paths:
        if path.endswith(".class"):
            chunk.append((path, open(path, "rb").read()))
        else:
            z = zipfile.ZipFile(path)
            for n in z.namelist():
                if n.endswith(".class"):
                    chunk.append((path+"!"+n, z.read(n)))
                    if len(chunk) >= size:
                        yield chunk; chunk = []
        if len(chunk) >= size:
            yield chunk; chunk = []
    if chunk:
        yield chunk

def main(args):
    sample = os.path.join(ROOT, "sample")
    if not args:
        args = [os.path.join(sample, "ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg.jar")]
    elif args == ["--all"]:
        args = sorted(os.path.join(d, f) for d, _, fs in os.walk(sample) for f in fs
                      if f.endswith((".jar", ".class")) and "/extracted/" not in d + "/")
    t0 = time.time()
    total = new_stats()
    with multiprocessing.Pool() as pool:
        for stats in pool.imap_unordered(check_chunk, chunks(args)):
            total["count"].update(stats["count"])
            total["classes"] += stats["classes"]
            for k in KEYS: total[k] += stats[k]
    print("classes: %d, instructions: %d, distinct opcodes: %d, %.1fs" % (total["classes"], sum(total["count"].values()), len(total["count"]), time.time()-t0))
    for key in KEYS:
        print("%s: %d" % (key, len(total[key])))
        for line in total[key][:10]: print("   ", line)
    unseen = [n for n in opcodes.InstructionNames if n and n not in total["count"]]
    print("opcodes not exercised by the input:", " ".join(unseen))
    return 0 if not any(total[k] for k in KEYS) else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
