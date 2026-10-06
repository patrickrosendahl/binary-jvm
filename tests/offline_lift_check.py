"""Offline sanity check for the plugin's decoder + lifter (no Binary Ninja licence needed).

Stubs out the `binaryninja` module, loads the plugin package, parses every .class in the given
.class/.jar files, linearly decodes every method and lifts each instruction into a recording mock IL.
Checks that:
  * every instruction decodes and the decoded lengths tile the method's code exactly,
  * no instruction lifts to `unimplemented`,
  * the operand-stack shape stackmap computes changes by the amount an independent JVM stack-effect
    table says (in 4-byte slots),
  * the lifted IL uses the stack registers st<k> consistently with that shape: it only reads entries the
    instruction pops (with their category's size), never reads a stack register it already wrote,
    writes exactly the entries it pushes (and nothing above the resulting depth), stack shuffles
    (dup*/swap) leave the right entry in every position, and the memory stack `s` only moves for
    entries >= NUM_STACK_REGS. Instructions without a stack shape (unreachable code; or everything
    with --nostate) must lift to the memory stack with the table's net effect.
Runs in parallel over chunks of classes.

usage: python3 tests/offline_lift_check.py [--nostate] [--all | file.jar|file.class ...]
       no arguments: quick check on sample/.../lib/mdg.jar;  --all: every .jar/.class under sample/
"""
import collections, importlib.util, multiprocessing, os, re, subprocess, sys, time, types, zipfile

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
from binary_jvm import opcodes, lifter, classfile, stackmap, methodinfo
from binary_jvm.constants import NUM_STACK_REGS, METHOD_BASE, METHOD_STRIDE

def sample_dir():
    sample = os.path.join(ROOT, "sample")
    if os.path.isdir(sample):
        return sample
    # in a git worktree the (git-ignored) samples live in the main checkout
    common = subprocess.run(["git", "-C", ROOT, "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    return os.path.join(os.path.dirname(os.path.abspath(os.path.join(ROOT, common))), "sample")

# ---- recording IL -------------------------------------------------------------------------------
ST = re.compile(r"st(\d+)(_lo)?$")

class MockIL:
    """records memory-stack movement, unimplemented, and stack-register reads/writes; register values
    are tracked symbolically (an unwritten st<k> reads as ("in", k, size)) so shuffles can be checked"""
    arch = None
    def __init__(self):
        self.delta = 0
        self.unimplemented_used = False
        self.vals = {}       # register (base name) / temp -> symbolic value written this instruction
        self.reads = []      # (entry, size) of stack registers read before being written
        self.writes = {}     # entry -> size of the last write
        self.errors = []
        self.other_reads = set()  # non-stack registers read
        self.targets = set()      # addresses the IL branches to
        self.handler_entry = None # what the handler-entry prologue wrote to the stack registers
    def append(self, expr): pass
    def mark_label(self, label): pass
    def get_label_for_address(self, arch, addr): self.targets.add(addr); return None
    def push(self, size, expr): self.delta += size; return ("push", size)
    def pop(self, size): self.delta -= size; return ("pop", size)
    def call_stack_adjust(self, dest, adjust): self.delta -= adjust; return ("call",)
    def unimplemented(self): self.unimplemented_used = True; return ("unimpl",)
    def reg(self, size, name):
        m = ST.match(name) if isinstance(name, str) else None
        if m:
            k = int(m.group(1))
            if k in self.writes:
                self.errors.append("reads st%d after writing it" % k)
                return self.vals.get(k)
            self.reads.append((k, size))
            return ("in", k, size)
        self.other_reads.add(name)
        return self.vals.get(name, ("reg", name))
    def _write(self, size, name, value):
        m = ST.match(name) if isinstance(name, str) else None
        if m:
            k = int(m.group(1))
            self.writes[k] = size
            self.vals[k] = value
        else:
            self.vals[name] = value
    def set_reg(self, size, name, value):
        self._write(size, name, value)
        return ("set_reg",)
    def intrinsic(self, outputs, name, params):
        assert name in lifter.INTRINSICS, name
        for out in outputs:
            self._write(None, out, ("intrinsic", name))
        return ("intrinsic", name)
    def __getattr__(self, name):
        return lambda *a, **k: (name,)

class Data:
    def __init__(self, b): self.b = b
    def read(self, off, n): return self.b[off:off+n]
    @property
    def length(self): return len(self.b)  # like BinaryView: .length, no __len__

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

SHUFFLES = set(stackmap._DUP) | set(stackmap._POP) | {"swap"}

def popped_entries(reader, name, value, state):
    """number of stack entries the instruction consumes"""
    if name == "wide": return popped_entries(reader, opcodes.InstructionNames[value[0]], value[1], state)
    if name in SHUFFLES: return len(state) - lifter.shuffle_layout(name, state)[0]
    if name in ("jsr", "jsr_w"): return 0
    if name == "multianewarray": return value[1]
    if name in stackmap._FIXED: return len(stackmap._FIXED[name][0])
    index = value[0] if isinstance(value, tuple) else value
    if name in ("getstatic", "putstatic", "getfield", "putfield"):
        return {"getstatic": 0, "putstatic": 1, "getfield": 1, "putfield": 2}[name]
    args, _ = opcodes.parse_method_descriptor(reader.memberDescriptor(index))
    return len(args) + (0 if name in ("invokestatic", "invokedynamic") else 1)

def check_stack_regs(reader, name, value, state, il, where, stats, fused=None):
    """the stack-register checks for an instruction with a known stack shape; returns error strings.
    fused: "cmp" / "if" for the two halves of a fused compare (the result entry is never materialised:
    the compare leaves its operands in cmpa/cmpb, the if reads them instead of popping)"""
    errors = list(il.errors)
    if fused == "cmp":
        regs = ("cmpa", "cmpb") if name in ("lcmp", "dcmpl", "dcmpg") else ("cmpa_lo", "cmpb_lo")
        if any(r not in il.vals for r in regs):
            errors.append("fused compare does not set %s/%s" % regs)
    elif fused == "if":
        if not ({"cmpa", "cmpb"} <= il.other_reads or {"cmpa_lo", "cmpb_lo"} <= il.other_reads):
            errors.append("fused if does not compare cmpa/cmpb")
    jsr = name in ("jsr", "jsr_w")
    out = stackmap._effect(name, value, state, reader.memberDescriptor)
    if jsr:
        out = state  # the fall-through sees the old stack; the return address goes to the subroutine
    slots = expected(reader, name, value)
    if sum(out) - sum(state) != slots:
        errors.append("stackmap effect %d slots, table says %d" % (sum(out) - sum(state), slots))
    first = len(state) - popped_entries(reader, name, value, state)
    for k, size in il.reads:
        if not (first <= k < len(state)) or k >= NUM_STACK_REGS:
            errors.append("reads st%d outside the popped entries %d..%d" % (k, first, len(state) - 1))
        elif size != 4 * state[k]:
            errors.append("reads st%d as %d bytes, category %d" % (k, size, state[k]))
    for k, size in il.writes.items():
        if jsr and k == len(state):
            continue
        if k >= len(out) or k >= NUM_STACK_REGS or k < first:
            errors.append("writes st%d outside the pushed entries %d..%d" % (k, first, len(out) - 1))
        elif size is not None and size != 4 * out[k]:
            errors.append("writes st%d as %d bytes, category %d" % (k, size, out[k]))
    if name in SHUFFLES:
        _, srcs = lifter.shuffle_layout(name, state)
        for i, s in enumerate(srcs):
            p = first + i
            if p >= NUM_STACK_REGS or s >= NUM_STACK_REGS:
                continue
            got = il.vals[p] if p in il.writes else ("in", p, 4 * state[p]) if p < len(state) else None
            if got != ("in", s, 4 * state[s]):
                errors.append("entry %d holds %r, expected entry %d" % (p, got, s))
    else:
        for k in range(first, min(len(out), NUM_STACK_REGS)):
            if k not in il.writes and fused != "cmp":
                errors.append("does not write pushed entry st%d" % k)
    if jsr:
        if len(state) < NUM_STACK_REGS and il.writes.get(len(state)) != 4:
            errors.append("jsr does not put the return address in st%d" % len(state))
        mem = 0
    else:
        mem = 4 * (sum(out[NUM_STACK_REGS:]) - sum(state[NUM_STACK_REGS:]))
    if il.delta != mem:
        errors.append("memory stack moves %d bytes, expected %d" % (il.delta, mem))
    if len(state) > NUM_STACK_REGS or len(out) > NUM_STACK_REGS:
        stats["deep"] += 1
    return errors

def check_fused_semantics():
    """every (compare, if) pair of the fusion table branches exactly when the JVM would, incl. NaN"""
    nan = float("nan")
    ordered = {"compare_equal": lambda a, b: a == b, "compare_not_equal": lambda a, b: a != b,
               "compare_signed_less_than": lambda a, b: a < b, "compare_signed_less_equal": lambda a, b: a <= b,
               "compare_signed_greater_than": lambda a, b: a > b, "compare_signed_greater_equal": lambda a, b: a >= b,
               "float_compare_equal": lambda a, b: a == b, "float_compare_less_than": lambda a, b: a < b,
               "float_compare_less_equal": lambda a, b: a <= b, "float_compare_greater_than": lambda a, b: a > b,
               "float_compare_greater_equal": lambda a, b: a >= b}  # Python float compares are ordered
    conds = {"ifeq": lambda r: r == 0, "ifne": lambda r: r != 0, "iflt": lambda r: r < 0,
             "ifge": lambda r: r >= 0, "ifgt": lambda r: r > 0, "ifle": lambda r: r <= 0}
    def jvm_cmp(cmp, a, b):
        if a != a or b != b:
            return -1 if cmp.endswith("l") else 1
        return (a > b) - (a < b)
    errors = []
    for (cmp, cond), (op, negate) in lifter.FUSED_COMPARE.items():
        pairs = [(1, 2), (2, 2), (3, 2)] + ([] if cmp == "lcmp" else [(nan, 1.0), (1.0, nan), (nan, nan)])
        for a, b in pairs:
            want = conds[cond](jvm_cmp(cmp, a, b))
            got = ordered[op](a, b) != negate
            if want != got:
                errors.append("%s+%s on (%r, %r): lifted %s, JVM %s" % (cmp, cond, a, b, got, want))
    return errors

# the handler-entry prologue (type test, st0_lo = exc) runs before the instruction's own lift: record what
# it did and start the instruction's stack-register bookkeeping afresh
_lift_handler_entry = lifter.lift_handler_entry
def _checked_handler_entry(il, ctx, off):
    _lift_handler_entry(il, ctx, off)
    il.handler_entry = (dict(il.writes), list(il.reads), il.delta)
    il.writes, il.reads, il.delta = {}, [], 0
    il.vals = {k: v for k, v in il.vals.items() if not isinstance(k, int)}
lifter.lift_handler_entry = _checked_handler_entry

def check_branches(name, value, addr, length, mi, il):
    errors = []
    branches = methodinfo.instruction_branches(name, value, addr, length, mi)
    if len(branches) > 3:
        errors.append("%d branches: %r" % (len(branches), branches))
    allowed = {t for _, t in branches} | {addr + length}
    if name in ("tableswitch", "lookupswitch"):  # IndirectBranch; the view sets the targets (analyze_tables)
        allowed |= {value[0]} | {t for _, t in value[3 if name == "tableswitch" else 2]}
    for t in il.targets - allowed:
        errors.append("IL branches to 0x%x, not among the instruction's branches %r" % (t, branches))
    return errors

# ---- driver -------------------------------------------------------------------------------------
KEYS = ("decode_fail", "lift_error", "unimplemented", "stack_mismatch", "stack_regs", "branches", "tiling")
NOSTATE = False

def check_class(label, raw, stats):
    reader = classfile.JVMClassReader(Dummy(), Data(raw))
    reader.charType = Dummy()
    cls = classfile.JVMClassStructure(reader)
    reader.classStruct = cls
    for m in cls.methods:
        if m.code_attribute is None: continue
        code = m.code_attribute.attribute
        base = METHOD_BASE + METHOD_STRIDE*m.index
        code_bytes = raw[code.start_address:code.end_address]
        off = 0
        while off < len(code_bytes):
            addr = base + off
            name, operand, length, value = opcodes.decode_instruction(code_bytes[off:], addr)
            if name is None:
                stats["decode_fail"].append("%s m%d +%x" % (label, m.index, off)); break
            where = "%s m%d +%x %s" % (label, m.index, off, name)
            il = MockIL()
            ctx = lifter.LiftContext(addr, length, reader)
            if NOSTATE:
                ctx.state = ctx.method = None
            mi = ctx.method
            fused = None if mi is None else "cmp" if off in mi.fused_cmp else "if" if off in mi.fused_if else None
            if fused:
                stats["fused"] += 1
            try:
                lifter.lift_instruction(il, name, value, ctx)
            except Exception as e:
                stats["lift_error"].append("%s: %r" % (where, e))
                off += length; continue
            stats["count"][name] += 1
            if il.unimplemented_used: stats["unimplemented"].append("%s %s" % (label, name))
            for e in check_branches(name, value, addr, length, mi, il):
                stats["branches"].append("%s: %s" % (where, e))
            if mi is not None and off in mi.throw_sites:
                stats["throw_sites"] += 1
            if il.handler_entry is not None:
                stats["handler_entries"] += 1
                writes, reads, delta = il.handler_entry
                if ctx.state is not None and (writes != {0: 4} or reads or delta):
                    stats["stack_regs"].append("%s: handler entry writes %r, reads %r, moves %d" % (where, writes, reads, delta))
            if ctx.state is None:
                stats["nostate"] += 1
                exp = expected(reader, name, value) * 4
                if il.delta != exp:
                    stats["stack_mismatch"].append("%s: lifted %d, expected %d" % (where, il.delta, exp))
                if il.reads or il.writes:
                    stats["stack_regs"].append("%s: uses stack registers without a stack shape" % where)
            else:
                try:
                    errors = check_stack_regs(reader, name, value, ctx.state, il, where, stats, fused)
                except Exception as e:
                    errors = ["check failed: %r" % e]
                for e in errors:
                    (stats["stack_mismatch"] if "table says" in e else stats["stack_regs"]).append("%s: %s (stack %r)" % (where, e, ctx.state))
            off += length
        if off != len(code_bytes):
            stats["tiling"].append("%s m%d" % (label, m.index))

def new_stats():
    stats = {"count": collections.Counter(), "classes": 0, "nostate": 0, "deep": 0, "fused": 0, "throw_sites": 0, "handler_entries": 0}
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

def chunks(paths, size=50):
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

def unpacked_jar_dir(d):
    """True inside foo/ when foo.jar sits next to it (the unpack convention) -- those classes are
    already checked via the jar. Also skips the legacy sample/extracted/."""
    parts = os.path.normpath(d).split(os.sep)
    if "extracted" in parts:
        return True
    for i in range(1, len(parts)):
        if os.path.exists(os.sep.join(parts[:i+1]) + ".jar"):
            return True
    return False

def _init_worker(nostate):
    global NOSTATE
    NOSTATE = nostate

def main(args):
    nostate = "--nostate" in args
    args = [a for a in args if a != "--nostate"]
    sample = sample_dir()
    if not args:
        args = [os.path.join(sample, "ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg.jar")]
    elif args == ["--all"]:
        args = sorted(os.path.join(d, f) for d, _, fs in os.walk(sample) for f in fs
                      if f.endswith((".jar", ".class")) and not unpacked_jar_dir(d))
    t0 = time.time()
    total = new_stats()
    with multiprocessing.Pool(initializer=_init_worker, initargs=(nostate,)) as pool:
        for stats in pool.imap_unordered(check_chunk, chunks(args)):
            total["count"].update(stats["count"])
            for k in ("classes", "nostate", "deep", "fused", "throw_sites", "handler_entries"):
                total[k] += stats[k]
            for k in KEYS: total[k] += stats[k]
    print("classes: %d, instructions: %d, distinct opcodes: %d, %.1fs" % (total["classes"], sum(total["count"].values()), len(total["count"]), time.time()-t0))
    print("instructions without a stack shape (memory stack): %d; touching entries >= %d: %d; fused compare/if halves: %d"
          % (total["nostate"], NUM_STACK_REGS, total["deep"], total["fused"]))
    print("exception checks after throw sites: %d; handler entries: %d" % (total["throw_sites"], total["handler_entries"]))
    errors = check_fused_semantics()
    print("fused compare semantics: %d errors" % len(errors))
    for line in errors[:10]: print("   ", line)
    for key in KEYS:
        print("%s: %d" % (key, len(total[key])))
        for line in total[key][:10]: print("   ", line)
    unseen = [n for n in opcodes.InstructionNames if n and n not in total["count"]]
    print("opcodes not exercised by the input:", " ".join(unseen))
    return 0 if not any(total[k] for k in KEYS) and not errors else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
