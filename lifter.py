"""LLIL lifting for every JVM opcode.

Model: each local slot n is register l<n> (8 bytes; l<n>_lo is its low 4 bytes). Operand-stack entry k
(counted in entries from the bottom; a long/double is one entry) is register st<k> (8 bytes) or
st<k>_lo (4 bytes), using the stack shape methodinfo/stackmap computed for the instruction. Entries
from NUM_STACK_REGS up, and the whole stack of instructions without a known shape (unreachable code,
no class reader), use the real stack on `s` (4-byte slots, long/double take two). Objects/fields/calls
are intrinsics or calls whose stack effects come from the constant-pool descriptors.
"""
from binaryninja import LLIL_TEMP, LowLevelILLabel, Type

from .constants import *
from .opcodes import InstructionNames, slot_size, parse_method_descriptor
from .classfile import (reader_for_view, JVMIntegerInfo, JVMFloatInfo, JVMLongInfo, JVMDoubleInfo)
from .methodinfo import method_info
from . import stackmap

T0, T1, T2 = LLIL_TEMP(0), LLIL_TEMP(1), LLIL_TEMP(2)
STACK_TEMP = 0x100  # temps for values popped off the memory stack / moved by stack shuffles

def reader_for_il(il):
    try:
        func = il.source_function
        if func is None:
            return None
        return reader_for_view(func.view)
    except Exception:
        return None

def arg_reg(index, size):
    # a<n> is the 8-byte register of outgoing argument n, a<n>_lo its low 4 bytes
    return ("a%d" if size == 8 else "a%d_lo") % index

def local_reg(index, size):
    # l<n> is the 8-byte register of local slot n, l<n>_lo its low 4 bytes
    return ("l%d" if size == 8 else "l%d_lo") % index

def stack_reg(index, size):
    # operand-stack entry n: st<n> when it holds a long/double, st<n>_lo (a separate 4-byte register) otherwise
    return ("st%d" if size == 8 else "st%d_lo") % index

def local_expr(il, index, size):
    if index < NUM_LOCAL_REGS:
        return il.reg(size, local_reg(index, size))
    return il.load(size, il.const_pointer(ADDR_SIZE, LOCALS_ADDR+index*4))

def set_local(il, index, size, value):
    if index < NUM_LOCAL_REGS:
        return il.set_reg(size, local_reg(index, size), value)
    return il.store(size, il.const_pointer(ADDR_SIZE, LOCALS_ADDR+index*4), value)

def pool_pointer(il, index):
    return il.const_pointer(ADDR_SIZE, pool_address(index))

def branch(il, target):
    label = il.get_label_for_address(il.arch, target)
    if label is not None:
        il.append(il.goto(label))
    else:
        il.append(il.jump(il.const_pointer(ADDR_SIZE, target)))

def branch_if(il, cond, target, negate=False):
    """goto target if cond (if not cond, when negate); falls through to whatever is lifted next otherwise"""
    t = il.get_label_for_address(il.arch, target)
    f = LowLevelILLabel()
    jump = t is None
    if jump:
        t = LowLevelILLabel()
    il.append(il.if_expr(cond, f, t) if negate else il.if_expr(cond, t, f))
    if jump:
        il.mark_label(t)
        il.append(il.jump(il.const_pointer(ADDR_SIZE, target)))
    il.mark_label(f)

class Stack:
    """The operand stack of one instruction. pop() returns a function building a fresh expression for
    the value (call it as often as needed); values popped from registers are read lazily, so lifters
    must use them before pushing over them (they all pop first and push last)."""
    def __init__(self, il, state):
        self.il = il
        self.cats = None if state is None else list(state)
        self.ntemp = 0

    def temp(self):
        self.ntemp += 1
        return LLIL_TEMP(STACK_TEMP + self.ntemp)

    def in_regs(self, entry):
        return self.cats is not None and entry < NUM_STACK_REGS

    def pop(self, size):
        il = self.il
        if self.cats is not None and self.cats:
            k = len(self.cats) - 1
            self.cats.pop()
            if k < NUM_STACK_REGS:
                name = stack_reg(k, size)
                return lambda: il.reg(size, name)
        t = self.temp()
        il.append(il.set_reg(size, t, il.pop(size)))
        return lambda: il.reg(size, t)

    def push_target(self, size):
        """register the next push writes (None: the memory stack); claims the entry"""
        if self.cats is None:
            return None
        k = len(self.cats)
        self.cats.append(2 if size == 8 else 1)
        return stack_reg(k, size) if k < NUM_STACK_REGS else None

    def push(self, size, expr):
        il = self.il
        name = self.push_target(size)
        if name is None:
            il.append(il.push(size, expr))
        else:
            il.append(il.set_reg(size, name, expr))

    def push_intrinsic(self, size, intrinsic, params):
        """push the single output of an intrinsic (straight into the stack register when there is one)"""
        il = self.il
        name = self.push_target(size)
        if name is None:
            t = self.temp()
            il.append(il.intrinsic([t], intrinsic, params))
            il.append(il.push(size, il.reg(size, t)))
        else:
            il.append(il.intrinsic([name], intrinsic, params))

class LiftContext():
    def __init__(self, addr, length, reader):
        self.addr = addr
        self.length = length
        self.reader = reader
        self.method = method_info(reader, addr)
        self.state = self.method.state(addr) if self.method is not None else None
        self.stack = None

    def pool(self, index):
        if self.reader is None:
            return None
        return self.reader.poolEntry(index)

    def descriptor(self, index):
        if self.reader is None:
            return None
        return self.reader.memberDescriptor(index)

    def pop(self, size):
        return self.stack.pop(size)

    def push(self, size, expr):
        self.stack.push(size, expr)

    def push_intrinsic(self, size, intrinsic, params):
        self.stack.push_intrinsic(size, intrinsic, params)

def lift_instruction(il, name, value, ctx):
    """lift one decoded instruction (the entry point for the architecture)"""
    ctx.stack = Stack(il, ctx.state)
    mi = ctx.method
    if mi is None:
        InstructionIL[name](il, value, ctx)
        return
    off = ctx.addr - mi.base
    if off in mi.dispatch or off in mi.conflicts:
        lift_handler_entry(il, ctx, off)
    if off in mi.fused_cmp:
        lift_cmp_fused(il, name, ctx)
    elif off in mi.fused_if:
        lift_if_fused(il, mi.fused_if[off], name, value, ctx)
    else:
        InstructionIL[name](il, value, ctx)
    chain = mi.throw_sites.get(off)
    if chain is not None and name != "athrow":
        # a throw site inside a try range: a pending exception goes to the first handler of the range.
        # exc is (re)defined explicitly: BN keeps a caller-saved register's variable across a call
        il.append(il.intrinsic(["exc"], "__exception", []))
        branch_if(il, il.compare_not_equal(ADDR_SIZE, il.reg(ADDR_SIZE, "exc"), il.const(ADDR_SIZE, 0)),
                  mi.base + chain[0][0])

# --- exceptions (jvm-42) ---
# A throw site in a try range is followed by `exc = __exception(); if (exc != 0) goto <first handler of
# its chain>`; athrow
# there is `exc = value; goto handler`. Each handler's first instruction tests the type:
# `if (!instanceof(exc, T)) goto <next handler>` (or `__propagate(exc)` + no_ret after the last one),
# then moves the exception onto the operand stack: `st0_lo = exc; exc = 0`.

def lift_handler_entry(il, ctx, off):
    mi = ctx.method
    exc = lambda: il.reg(ADDR_SIZE, "exc")
    if off in mi.conflicts:
        # reached through chains that continue differently (never seen in javac output): the intrinsic
        # yields the next handler to try (0: caught here), the view lists the candidates as indirect branches
        il.append(il.intrinsic([T0], "__catch_next", [exc()]))
        go, caught = LowLevelILLabel(), LowLevelILLabel()
        il.append(il.if_expr(il.compare_not_equal(ADDR_SIZE, il.reg(ADDR_SIZE, T0), il.const(ADDR_SIZE, 0)), go, caught))
        il.mark_label(go)
        il.append(il.jump(il.reg(ADDR_SIZE, T0)))
        il.mark_label(caught)
    else:
        types, nxt = mi.dispatch[off]
        if types:
            temps = [LLIL_TEMP(i) for i in range(len(types))]
            for t, name in zip(temps, types):
                il.append(il.intrinsic([t], "instanceof", [exc(), pool_pointer(il, mi.class_index[name])]))
            caught = il.reg(4, temps[0])
            for t in temps[1:]:
                caught = il.or_expr(4, caught, il.reg(4, t))
            not_caught = il.compare_equal(4, caught, il.const(4, 0))
            if nxt is None:
                # the last handler of the chain: anything else leaves the method
                prop, cont = LowLevelILLabel(), LowLevelILLabel()
                il.append(il.if_expr(not_caught, prop, cont))
                il.mark_label(prop)
                il.append(il.intrinsic([], "__propagate", [exc()]))
                il.append(il.no_ret())
                il.mark_label(cont)
            else:
                branch_if(il, not_caught, mi.base + nxt)
    # the exception is the handler's operand stack
    if ctx.state is not None and len(ctx.state) == 1:
        il.append(il.set_reg(ADDR_SIZE, stack_reg(0, ADDR_SIZE), exc()))
    else:
        il.append(il.push(ADDR_SIZE, exc()))
    il.append(il.set_reg(ADDR_SIZE, "exc", il.const(ADDR_SIZE, 0)))

# --- lifters: each takes (il, operand, ctx) and appends its own instructions ---

def lift_const(size, value):
    if isinstance(value, float):
        make = (lambda il: il.float_const_single(value)) if size == 4 else (lambda il: il.float_const_double(value))
    else:
        make = lambda il: il.const(size, value)
    return lambda il, v, ctx: ctx.push(size, make(il))

def lift_ldc(size):
    def lift(il, index, ctx):
        entry = ctx.pool(index)
        if isinstance(entry, JVMIntegerInfo):
            ctx.push(4, il.const(4, signed(entry.value, 32)))
        elif isinstance(entry, JVMFloatInfo):
            ctx.push(4, il.float_const_single(entry.value))
        elif isinstance(entry, JVMLongInfo):
            ctx.push(8, il.const(8, signed(entry.value, 64)))
        elif isinstance(entry, JVMDoubleInfo):
            ctx.push(8, il.float_const_double(entry.value))
        elif size == 4:
            # String / Class / MethodType / MethodHandle / Dynamic: a reference to the pool entry
            ctx.push(4, pool_pointer(il, index))
        else:
            ctx.push(8, il.load(8, pool_pointer(il, index)))
    return lift

def signed(value, bits):
    return value - (1 << bits) if value >= (1 << (bits-1)) else value

def lift_load_local(size, index=None):
    return lambda il, v, ctx: ctx.push(size, local_expr(il, v if index is None else index, size))

def lift_store_local(size, index=None):
    return lambda il, v, ctx: il.append(set_local(il, v if index is None else index, size, ctx.pop(size)()))

def element_address(il, arrayref, index, elem):
    return il.add(ADDR_SIZE, arrayref(), il.mult(ADDR_SIZE, index(), il.const(ADDR_SIZE, elem)))

def lift_array_load(elem, size, extend=None):
    def lift(il, v, ctx):
        index = ctx.pop(4)
        arrayref = ctx.pop(ADDR_SIZE)
        value = il.load(elem, element_address(il, arrayref, index, elem))
        if extend is not None:
            value = getattr(il, extend)(size, value)
        ctx.push(size, value)
    return lift

def lift_array_store(elem, size):
    def lift(il, v, ctx):
        value = ctx.pop(size)
        index = ctx.pop(4)
        arrayref = ctx.pop(ADDR_SIZE)
        value = value()
        if elem < size:
            value = il.low_part(elem, value)
        il.append(il.store(elem, element_address(il, arrayref, index, elem), value))
    return lift

# stack shuffles without a known stack shape: sizes popped into temps (top first), temps pushed in order
_SHUFFLE_FALLBACK = {
    "pop": ([4], []), "pop2": ([8], []), "dup": ([4], [0, 0]), "dup_x1": ([4, 4], [0, 1, 0]),
    "dup_x2": ([4, 8], [0, 1, 0]), "dup2": ([8], [0, 0]), "dup2_x1": ([8, 4], [0, 1, 0]),
    "dup2_x2": ([8, 8], [0, 1, 0]), "swap": ([4, 4], [0, 1]),
}

def shuffle_layout(name, state):
    """-> (first entry the instruction touches, source entry of each entry from there up afterwards)"""
    if name in stackmap._POP:
        rest, _ = stackmap._take_slots(state, stackmap._POP[name])
        return len(rest), []
    if name == "swap":
        r = len(stackmap._pop(state, (1, 1)))
        return r, [r + 1, r]
    n, m = stackmap._DUP[name]
    rest, top = stackmap._take_slots(state, n)
    rest, below = stackmap._take_slots(rest, m)
    r, b, t = len(rest), len(below), len(top)
    tops = list(range(r + b, r + b + t))
    return r, tops + list(range(r, r + b)) + tops

def lift_stack_shuffle(name):
    def lift(il, v, ctx):
        state = ctx.state
        if state is None:
            pops, pushes = _SHUFFLE_FALLBACK[name]
            temps = [ctx.stack.temp() for _ in pops]
            for i, size in enumerate(pops):
                il.append(il.set_reg(size, temps[i], il.pop(size)))
            for i in pushes:
                il.append(il.push(pops[i], il.reg(pops[i], temps[i])))
            if not pushes:
                il.append(il.nop())
            return
        try:
            first, srcs = shuffle_layout(name, state)
        except stackmap.StackError:
            il.append(il.unimplemented())
            return
        size = lambda entry: 4 * state[entry]
        if len(state) <= NUM_STACK_REGS and first + len(srcs) <= NUM_STACK_REGS:
            # register moves; sources that get overwritten are saved in temps first
            writes = [(first + i, s) for i, s in enumerate(srcs) if first + i != s]
            written = {p for p, _ in writes}
            saved = {}
            for _, s in writes:
                if s in written and s not in saved:
                    saved[s] = ctx.stack.temp()
                    il.append(il.set_reg(size(s), saved[s], il.reg(size(s), stack_reg(s, size(s)))))
            for p, s in writes:
                src = saved[s] if s in saved else stack_reg(s, size(s))
                il.append(il.set_reg(size(s), stack_reg(p, size(s)), il.reg(size(s), src)))
            if not writes:
                il.append(il.nop())
            return
        # deep stack: pop the touched entries into temps, push them back in the new order
        temps = {}
        for entry in reversed(range(first, len(state))):
            value = ctx.pop(size(entry))
            temps[entry] = ctx.stack.temp()
            il.append(il.set_reg(size(entry), temps[entry], value()))
        for s in srcs:
            ctx.push(size(s), il.reg(size(s), temps[s]))
    return lift

def lift_binop(op, size, rsize=None, mask=None):
    rsize = rsize or size
    def lift(il, v, ctx):
        b = ctx.pop(rsize)  # value2
        a = ctx.pop(size)   # value1
        rhs = b()
        if mask is not None:
            rhs = il.and_expr(rsize, rhs, il.const(rsize, mask))
        ctx.push(size, getattr(il, op)(size, a(), rhs))
    return lift

def lift_unop(op, size):
    return lambda il, v, ctx: ctx.push(size, getattr(il, op)(size, ctx.pop(size)()))

def lift_fmod(size):
    def lift(il, v, ctx):
        b = ctx.pop(size)
        a = ctx.pop(size)
        ctx.push_intrinsic(size, "fmod", [a(), b()])
    return lift

def lift_convert(src, dst, op):
    return lambda il, v, ctx: ctx.push(dst, getattr(il, op)(dst, ctx.pop(src)()))

def lift_narrow(part, extend):
    return lambda il, v, ctx: ctx.push(4, getattr(il, extend)(4, il.low_part(part, ctx.pop(4)())))

def lift_iinc(il, v, ctx):
    index, const = v
    il.append(set_local(il, index, 4, il.add(4, local_expr(il, index, 4), il.const(4, const))))

def lift_lcmp(il, v, ctx):
    # (a > b) - (a < b)
    b = ctx.pop(8)
    a = ctx.pop(8)
    ctx.push(4, il.sub(4, il.bool_to_int(4, il.compare_signed_greater_than(8, a(), b())),
                          il.bool_to_int(4, il.compare_signed_less_than(8, a(), b()))))

def lift_fcmp(size, nan_result):
    def lift(il, v, ctx):
        b = ctx.pop(size)
        a = ctx.pop(size)
        b2i = lambda cond: il.bool_to_int(4, cond)
        if nan_result < 0:
            # fcmpl: (a > b) + (a >= b) - 1   -> NaN gives -1
            result = il.sub(4, il.add(4, b2i(il.float_compare_greater_than(size, a(), b())),
                                         b2i(il.float_compare_greater_equal(size, a(), b()))), il.const(4, 1))
        else:
            # fcmpg: 1 - (a <= b) - (a < b)   -> NaN gives 1
            result = il.sub(4, il.sub(4, il.const(4, 1), b2i(il.float_compare_less_equal(size, a(), b()))),
                               b2i(il.float_compare_less_than(size, a(), b())))
        ctx.push(4, result)
    return lift

# compare fusion (jvm-32): `lcmp; ifge L` lifts as `cmpa = a; cmpb = b` + `if (cmpa >= cmpb) goto L`.
# (compare, if) -> (LLIL comparison of a and b, negate): the branch is taken iff the comparison holds
# (fails, when negated). Float comparisons are ordered (false on NaN); fcmpl/dcmpl make NaN -1 and
# fcmpg/dcmpg make it 1, so the conditions NaN satisfies are the negation of an ordered compare.
_IF_OPS = {"ifeq": "eq", "ifne": "ne", "iflt": "lt", "ifge": "ge", "ifgt": "gt", "ifle": "le"}
_LCMP = {"eq": ("compare_equal", False), "ne": ("compare_not_equal", False),
         "lt": ("compare_signed_less_than", False), "ge": ("compare_signed_greater_equal", False),
         "gt": ("compare_signed_greater_than", False), "le": ("compare_signed_less_equal", False)}
_FCMPL = {"eq": ("float_compare_equal", False), "ne": ("float_compare_equal", True),
          "lt": ("float_compare_greater_equal", True), "ge": ("float_compare_greater_equal", False),
          "gt": ("float_compare_greater_than", False), "le": ("float_compare_greater_than", True)}
_FCMPG = {"eq": ("float_compare_equal", False), "ne": ("float_compare_equal", True),
          "lt": ("float_compare_less_than", False), "ge": ("float_compare_less_than", True),
          "gt": ("float_compare_less_equal", True), "le": ("float_compare_less_equal", False)}
FUSED_COMPARE = {}
for _cmp, _table in (("lcmp", _LCMP), ("fcmpl", _FCMPL), ("dcmpl", _FCMPL), ("fcmpg", _FCMPG), ("dcmpg", _FCMPG)):
    for _if, _op in _IF_OPS.items():
        FUSED_COMPARE[(_cmp, _if)] = _table[_op]
COMPARE_SIZE = {"lcmp": 8, "fcmpl": 4, "fcmpg": 4, "dcmpl": 8, "dcmpg": 8}

def compare_regs(size):
    return ("cmpa", "cmpb") if size == 8 else ("cmpa_lo", "cmpb_lo")

def lift_cmp_fused(il, name, ctx):
    size = COMPARE_SIZE[name]
    ra, rb = compare_regs(size)
    b = ctx.pop(size)
    a = ctx.pop(size)
    il.append(il.set_reg(size, ra, a()))
    il.append(il.set_reg(size, rb, b()))

def lift_if_fused(il, cmp, name, target, ctx):
    size = COMPARE_SIZE[cmp]
    ra, rb = compare_regs(size)
    op, negate = FUSED_COMPARE[(cmp, name)]
    branch_if(il, getattr(il, op)(size, il.reg(size, ra), il.reg(size, rb)), target, negate)

def lift_if_zero(cmp):
    def lift(il, v, ctx):
        a = ctx.pop(4)
        branch_if(il, getattr(il, cmp)(4, a(), il.const(4, 0)), v)
    return lift

def lift_if_cmp(cmp, size=4):
    def lift(il, v, ctx):
        b = ctx.pop(size)
        a = ctx.pop(size)
        branch_if(il, getattr(il, cmp)(size, a(), b()), v)
    return lift

def lift_goto(il, v, ctx):
    branch(il, v)

def lift_jsr(il, v, ctx):
    # subroutine modelled as a call; the return address it finds on the stack is a plain constant (as a
    # const_pointer BN would start a bogus function at the return address)
    ret = il.const(ADDR_SIZE, ctx.addr+ctx.length)
    name = ctx.stack.push_target(ADDR_SIZE)
    if name is not None:
        il.append(il.set_reg(ADDR_SIZE, name, ret))
        il.append(il.call(il.const_pointer(ADDR_SIZE, v)))
    else:
        # memory stack: the "call" pops the pushed return address itself
        il.append(il.push(ADDR_SIZE, ret))
        il.append(il.call_stack_adjust(il.const_pointer(ADDR_SIZE, v), ADDR_SIZE))

def lift_ret(il, v, ctx):
    il.append(il.ret(local_expr(il, v, ADDR_SIZE)))

def lift_switch(cases_index):
    def lift(il, v, ctx):
        key = ctx.pop(4)
        for k, target in v[cases_index]:
            branch_if(il, il.compare_equal(4, key(), il.const(4, k)), target)
        branch(il, v[0])
    return lift

def lift_return(size):
    def lift(il, v, ctx):
        if size == 4:
            il.append(il.set_reg(4, "r", ctx.pop(4)()))
        elif size == 8:
            il.append(il.set_reg(8, "r64", ctx.pop(8)()))
        il.append(il.ret(il.reg(ADDR_SIZE, "lr")))
    return lift

def field_type(desc):
    # matches the access sizes used by getstatic/putstatic (sub-int types are widened to int)
    if desc[0] == 'J': return Type.int(8)
    if desc[0] == 'D': return Type.float(8)
    if desc[0] == 'F': return Type.float(4)
    if desc[0] in 'L[': return Type.pointer_of_width(ADDR_SIZE, Type.void())
    return Type.int(4)

def field_size(ctx, index):
    desc = ctx.descriptor(index)
    return slot_size(desc[0]) if desc else 4

def lift_getstatic(il, v, ctx):
    size = field_size(ctx, v)
    ctx.push(size, il.load(size, pool_pointer(il, v)))

def lift_putstatic(il, v, ctx):
    size = field_size(ctx, v)
    il.append(il.store(size, pool_pointer(il, v), ctx.pop(size)()))

def lift_getfield(il, v, ctx):
    size = field_size(ctx, v)
    obj = ctx.pop(ADDR_SIZE)
    ctx.push_intrinsic(size, "getfield", [obj(), pool_pointer(il, v)])

def lift_putfield(il, v, ctx):
    size = field_size(ctx, v)
    value = ctx.pop(size)
    obj = ctx.pop(ADDR_SIZE)
    il.append(il.intrinsic([], "putfield", [obj(), pool_pointer(il, v), value()]))

def lift_invoke(kind):
    def lift(il, v, ctx):
        index = v[0] if isinstance(v, tuple) else v
        desc = ctx.descriptor(index)
        if desc is None:
            # the stack effect is unknowable without the constant pool
            il.append(il.unimplemented())
            return
        args, ret = parse_method_descriptor(desc)
        sizes = [slot_size(a) for a in args]
        if kind not in ("invokestatic", "invokedynamic"):
            sizes = [ADDR_SIZE] + sizes  # objectref
        if INVOKES_AS_CALLS and len(sizes) <= NUM_ARG_REGS:
            # a real call through the pool entry, which the view types as a function pointer (like an
            # import table slot): arguments go to a<i> (receiver first), the result comes back in r / r64
            for i in reversed(range(len(sizes))):
                il.append(il.set_reg(sizes[i], arg_reg(i, sizes[i]), ctx.pop(sizes[i])()))
            il.append(il.call(il.load(ADDR_SIZE, pool_pointer(il, index))))
            if ret != 'V':
                if slot_size(ret) == 8:
                    ctx.push(8, il.reg(8, "r64"))
                else:
                    ctx.push(4, il.reg(4, "r"))
            return
        # too many arguments for the argument registers: fall back to an intrinsic
        values = [ctx.pop(sizes[i]) for i in reversed(range(len(sizes)))][::-1]
        params = [pool_pointer(il, index)] + [value() for value in values]
        if ret == 'V':
            il.append(il.intrinsic([], kind, params))
        else:
            ctx.push_intrinsic(slot_size(ret), kind, params)
    return lift

def lift_new(il, v, ctx):
    ctx.push_intrinsic(ADDR_SIZE, "new", [pool_pointer(il, v)])

def lift_newarray(il, v, ctx):
    count = ctx.pop(4)
    ctx.push_intrinsic(ADDR_SIZE, "newarray", [il.const_pointer(ADDR_SIZE, PSEUDOMEMORY_PRIMITIVES+v), count()])

def lift_anewarray(il, v, ctx):
    count = ctx.pop(4)
    ctx.push_intrinsic(ADDR_SIZE, "anewarray", [pool_pointer(il, v), count()])

def lift_multianewarray(il, v, ctx):
    index, dims = v
    counts = [ctx.pop(4) for _ in range(dims)][::-1]
    ctx.push_intrinsic(ADDR_SIZE, "multianewarray", [pool_pointer(il, index)] + [c() for c in counts])

def lift_object_op(name, with_class, size):
    # pops an objectref, optionally passes the class operand, pushes a result of `size` (0 = none)
    def lift(il, v, ctx):
        obj = ctx.pop(ADDR_SIZE)
        params = [obj()]
        if with_class:
            params.append(pool_pointer(il, v))
        if size == 0:
            il.append(il.intrinsic([], name, params))
        else:
            ctx.push_intrinsic(size, name, params)
    return lift

def lift_athrow(il, v, ctx):
    obj = ctx.pop(ADDR_SIZE)
    mi = ctx.method
    chain = mi.throw_sites.get(ctx.addr - mi.base) if mi is not None else None
    if chain is not None:
        il.append(il.set_reg(ADDR_SIZE, "exc", obj()))
        branch(il, mi.base + chain[0][0])
    else:
        il.append(il.intrinsic([], "__propagate", [obj()]))
        il.append(il.no_ret())

def lift_wide(il, v, ctx):
    name = InstructionNames[v[0]]
    if name == "iinc":
        lift_iinc(il, (v[1], v[2]), ctx)
    else:
        InstructionIL[name](il, v[1], ctx)

INTRINSICS = ["invokevirtual", "invokespecial", "invokestatic", "invokeinterface", "invokedynamic",
              "getfield", "putfield", "new", "newarray", "anewarray", "multianewarray", "arraylength",
              "checkcast", "instanceof", "monitorenter", "monitorexit", "fmod", "__exception", "__propagate", "__catch_next"]

InstructionIL = {
    "nop":         lambda il, v, ctx: il.append(il.nop()),
    "aconst_null": lambda il, v, ctx: ctx.push(ADDR_SIZE, il.const(ADDR_SIZE, 0)),
    "iconst_m1":   lift_const(4, -1),
    "iconst_0":    lift_const(4, 0),
    "iconst_1":    lift_const(4, 1),
    "iconst_2":    lift_const(4, 2),
    "iconst_3":    lift_const(4, 3),
    "iconst_4":    lift_const(4, 4),
    "iconst_5":    lift_const(4, 5),
    "lconst_0":    lift_const(8, 0),
    "lconst_1":    lift_const(8, 1),
    "fconst_0":    lift_const(4, 0.0),
    "fconst_1":    lift_const(4, 1.0),
    "fconst_2":    lift_const(4, 2.0),
    "dconst_0":    lift_const(8, 0.0),
    "dconst_1":    lift_const(8, 1.0),
    "bipush":      lambda il, v, ctx: ctx.push(4, il.const(4, v)),
    "sipush":      lambda il, v, ctx: ctx.push(4, il.const(4, v)),
    "ldc":         lift_ldc(4),
    "ldc_w":       lift_ldc(4),
    "ldc2_w":      lift_ldc(8),
    "iinc":        lift_iinc,

    "iaload":      lift_array_load(4, 4),
    "laload":      lift_array_load(8, 8),
    "faload":      lift_array_load(4, 4),
    "daload":      lift_array_load(8, 8),
    "aaload":      lift_array_load(ADDR_SIZE, ADDR_SIZE),
    "baload":      lift_array_load(1, 4, "sign_extend"),
    "caload":      lift_array_load(2, 4, "zero_extend"),
    "saload":      lift_array_load(2, 4, "sign_extend"),
    "iastore":     lift_array_store(4, 4),
    "lastore":     lift_array_store(8, 8),
    "fastore":     lift_array_store(4, 4),
    "dastore":     lift_array_store(8, 8),
    "aastore":     lift_array_store(ADDR_SIZE, ADDR_SIZE),
    "bastore":     lift_array_store(1, 4),
    "castore":     lift_array_store(2, 4),
    "sastore":     lift_array_store(2, 4),

    # operand stack: register moves using the known categories
    "pop":         lift_stack_shuffle("pop"),
    "pop2":        lift_stack_shuffle("pop2"),
    "dup":         lift_stack_shuffle("dup"),
    "dup_x1":      lift_stack_shuffle("dup_x1"),
    "dup_x2":      lift_stack_shuffle("dup_x2"),
    "dup2":        lift_stack_shuffle("dup2"),
    "dup2_x1":     lift_stack_shuffle("dup2_x1"),
    "dup2_x2":     lift_stack_shuffle("dup2_x2"),
    "swap":        lift_stack_shuffle("swap"),

    "iadd":        lift_binop("add", 4),
    "ladd":        lift_binop("add", 8),
    "fadd":        lift_binop("float_add", 4),
    "dadd":        lift_binop("float_add", 8),
    "isub":        lift_binop("sub", 4),
    "lsub":        lift_binop("sub", 8),
    "fsub":        lift_binop("float_sub", 4),
    "dsub":        lift_binop("float_sub", 8),
    "imul":        lift_binop("mult", 4),
    "lmul":        lift_binop("mult", 8),
    "fmul":        lift_binop("float_mult", 4),
    "dmul":        lift_binop("float_mult", 8),
    "idiv":        lift_binop("div_signed", 4),
    "ldiv":        lift_binop("div_signed", 8),
    "fdiv":        lift_binop("float_div", 4),
    "ddiv":        lift_binop("float_div", 8),
    "irem":        lift_binop("mod_signed", 4),
    "lrem":        lift_binop("mod_signed", 8),
    "frem":        lift_fmod(4),
    "drem":        lift_fmod(8),
    "ineg":        lift_unop("neg_expr", 4),
    "lneg":        lift_unop("neg_expr", 8),
    "fneg":        lift_unop("float_neg", 4),
    "dneg":        lift_unop("float_neg", 8),
    "ishl":        lift_binop("shift_left", 4, mask=0x1f),
    "lshl":        lift_binop("shift_left", 8, 4, 0x3f),
    "ishr":        lift_binop("arith_shift_right", 4, mask=0x1f),
    "lshr":        lift_binop("arith_shift_right", 8, 4, 0x3f),
    "iushr":       lift_binop("logical_shift_right", 4, mask=0x1f),
    "lushr":       lift_binop("logical_shift_right", 8, 4, 0x3f),
    "iand":        lift_binop("and_expr", 4),
    "land":        lift_binop("and_expr", 8),
    "ior":         lift_binop("or_expr", 4),
    "lor":         lift_binop("or_expr", 8),
    "ixor":        lift_binop("xor_expr", 4),
    "lxor":        lift_binop("xor_expr", 8),

    "i2l":         lift_convert(4, 8, "sign_extend"),
    "i2f":         lift_convert(4, 4, "int_to_float"),
    "i2d":         lift_convert(4, 8, "int_to_float"),
    "l2i":         lift_convert(8, 4, "low_part"),
    "l2f":         lift_convert(8, 4, "int_to_float"),
    "l2d":         lift_convert(8, 8, "int_to_float"),
    "f2i":         lift_convert(4, 4, "float_to_int"),
    "f2l":         lift_convert(4, 8, "float_to_int"),
    "f2d":         lift_convert(4, 8, "float_convert"),
    "d2i":         lift_convert(8, 4, "float_to_int"),
    "d2l":         lift_convert(8, 8, "float_to_int"),
    "d2f":         lift_convert(8, 4, "float_convert"),
    "i2b":         lift_narrow(1, "sign_extend"),
    "i2c":         lift_narrow(2, "zero_extend"),
    "i2s":         lift_narrow(2, "sign_extend"),

    "lcmp":        lift_lcmp,
    "fcmpl":       lift_fcmp(4, -1),
    "fcmpg":       lift_fcmp(4, 1),
    "dcmpl":       lift_fcmp(8, -1),
    "dcmpg":       lift_fcmp(8, 1),

    "ifeq":        lift_if_zero("compare_equal"),
    "ifne":        lift_if_zero("compare_not_equal"),
    "iflt":        lift_if_zero("compare_signed_less_than"),
    "ifge":        lift_if_zero("compare_signed_greater_equal"),
    "ifgt":        lift_if_zero("compare_signed_greater_than"),
    "ifle":        lift_if_zero("compare_signed_less_equal"),
    "if_icmpeq":   lift_if_cmp("compare_equal"),
    "if_icmpne":   lift_if_cmp("compare_not_equal"),
    "if_icmplt":   lift_if_cmp("compare_signed_less_than"),
    "if_icmpge":   lift_if_cmp("compare_signed_greater_equal"),
    "if_icmpgt":   lift_if_cmp("compare_signed_greater_than"),
    "if_icmple":   lift_if_cmp("compare_signed_less_equal"),
    "if_acmpeq":   lift_if_cmp("compare_equal", ADDR_SIZE),
    "if_acmpne":   lift_if_cmp("compare_not_equal", ADDR_SIZE),
    "ifnull":      lift_if_zero("compare_equal"),
    "ifnonnull":   lift_if_zero("compare_not_equal"),
    "goto":        lift_goto,
    "goto_w":      lift_goto,
    "jsr":         lift_jsr,
    "jsr_w":       lift_jsr,
    "ret":         lift_ret,
    "tableswitch": lift_switch(3),
    "lookupswitch": lift_switch(2),

    "ireturn":     lift_return(4),
    "lreturn":     lift_return(8),
    "freturn":     lift_return(4),
    "dreturn":     lift_return(8),
    "areturn":     lift_return(ADDR_SIZE),
    "return":      lift_return(0),

    "getstatic":   lift_getstatic,
    "putstatic":   lift_putstatic,
    "getfield":    lift_getfield,
    "putfield":    lift_putfield,
    "invokevirtual":   lift_invoke("invokevirtual"),
    "invokespecial":   lift_invoke("invokespecial"),
    "invokestatic":    lift_invoke("invokestatic"),
    "invokeinterface": lift_invoke("invokeinterface"),
    "invokedynamic":   lift_invoke("invokedynamic"),
    "new":         lift_new,
    "newarray":    lift_newarray,
    "anewarray":   lift_anewarray,
    "multianewarray": lift_multianewarray,
    "arraylength": lift_object_op("arraylength", False, 4),
    "athrow":      lift_athrow,
    "checkcast":   lift_object_op("checkcast", True, ADDR_SIZE),
    "instanceof":  lift_object_op("instanceof", True, 4),
    "monitorenter": lift_object_op("monitorenter", False, 0),
    "monitorexit": lift_object_op("monitorexit", False, 0),
    "wide":        lift_wide,
}

for _kind, _size in (("i", 4), ("l", 8), ("f", 4), ("d", 8), ("a", ADDR_SIZE)):
    InstructionIL[_kind+"load"] = lift_load_local(_size)
    InstructionIL[_kind+"store"] = lift_store_local(_size)
    for _n in range(4):
        InstructionIL["%sload_%d" % (_kind, _n)] = lift_load_local(_size, _n)
        InstructionIL["%sstore_%d" % (_kind, _n)] = lift_store_local(_size, _n)

