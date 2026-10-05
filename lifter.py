"""LLIL lifting for every JVM opcode.

Model: each local slot n is register l<n> (8 bytes; l<n>_lo is its low 4 bytes), the operand stack is the
real stack (4-byte slots, long/double take two), objects/fields/calls are intrinsics whose stack effects
come from the constant-pool descriptors.
"""
from binaryninja import LLIL_TEMP, LowLevelILLabel, Type

from .constants import *
from .opcodes import InstructionNames, slot_size, parse_method_descriptor
from .classfile import (reader_for_view, JVMIntegerInfo, JVMFloatInfo, JVMLongInfo, JVMDoubleInfo)

T0, T1, T2 = LLIL_TEMP(0), LLIL_TEMP(1), LLIL_TEMP(2)

def reader_for_il(il):
    try:
        func = il.source_function
        if func is None:
            return None
        return reader_for_view(func.view)
    except Exception:
        return None

def local_reg(index, size):
    # l<n> is the 8-byte register of local slot n, l<n>_lo its low 4 bytes
    return ("l%d" if size == 8 else "l%d_lo") % index

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

def push(il, size, expr):
    il.append(il.push(size, expr))

def pop_temp(il, temp, size):
    il.append(il.set_reg(size, temp, il.pop(size)))

def branch(il, target):
    label = il.get_label_for_address(il.arch, target)
    if label is not None:
        il.append(il.goto(label))
    else:
        il.append(il.jump(il.const_pointer(ADDR_SIZE, target)))

def branch_if(il, cond, target):
    # falls through to whatever is lifted next when cond is false
    t = il.get_label_for_address(il.arch, target)
    f = LowLevelILLabel()
    if t is not None:
        il.append(il.if_expr(cond, t, f))
    else:
        t = LowLevelILLabel()
        il.append(il.if_expr(cond, t, f))
        il.mark_label(t)
        il.append(il.jump(il.const_pointer(ADDR_SIZE, target)))
    il.mark_label(f)

class LiftContext():
    def __init__(self, addr, length, reader):
        self.addr = addr
        self.length = length
        self.reader = reader

    def pool(self, index):
        if self.reader is None:
            return None
        return self.reader.poolEntry(index)

    def descriptor(self, index):
        if self.reader is None:
            return None
        return self.reader.memberDescriptor(index)

# --- lifters: each takes (il, operand, ctx) and appends its own instructions ---

def lift_const(size, value):
    if isinstance(value, float):
        make = (lambda il: il.float_const_single(value)) if size == 4 else (lambda il: il.float_const_double(value))
    else:
        make = lambda il: il.const(size, value)
    return lambda il, v, ctx: push(il, size, make(il))

def lift_ldc(size):
    def lift(il, index, ctx):
        entry = ctx.pool(index)
        if isinstance(entry, JVMIntegerInfo):
            push(il, 4, il.const(4, signed(entry.value, 32)))
        elif isinstance(entry, JVMFloatInfo):
            push(il, 4, il.float_const_single(entry.value))
        elif isinstance(entry, JVMLongInfo):
            push(il, 8, il.const(8, signed(entry.value, 64)))
        elif isinstance(entry, JVMDoubleInfo):
            push(il, 8, il.float_const_double(entry.value))
        elif size == 4:
            # String / Class / MethodType / MethodHandle / Dynamic: a reference to the pool entry
            push(il, 4, pool_pointer(il, index))
        else:
            push(il, 8, il.load(8, pool_pointer(il, index)))
    return lift

def signed(value, bits):
    return value - (1 << bits) if value >= (1 << (bits-1)) else value

def lift_load_local(size, index=None):
    return lambda il, v, ctx: push(il, size, local_expr(il, v if index is None else index, size))

def lift_store_local(size, index=None):
    return lambda il, v, ctx: il.append(set_local(il, v if index is None else index, size, il.pop(size)))

def element_address(il, elem):
    return il.add(ADDR_SIZE, il.reg(ADDR_SIZE, T0), il.mult(ADDR_SIZE, il.reg(4, T1), il.const(ADDR_SIZE, elem)))

def lift_array_load(elem, size, extend=None):
    def lift(il, v, ctx):
        pop_temp(il, T1, 4)          # index
        pop_temp(il, T0, ADDR_SIZE)  # arrayref
        value = il.load(elem, element_address(il, elem))
        if extend is not None:
            value = getattr(il, extend)(size, value)
        push(il, size, value)
    return lift

def lift_array_store(elem, size):
    def lift(il, v, ctx):
        pop_temp(il, T2, size)       # value
        pop_temp(il, T1, 4)          # index
        pop_temp(il, T0, ADDR_SIZE)  # arrayref
        value = il.reg(size, T2)
        if elem < size:
            value = il.low_part(elem, value)
        il.append(il.store(elem, element_address(il, elem), value))
    return lift

def lift_stack_shuffle(pops, pushes):
    # pops: sizes popped into T0, T1, ... (top of stack first); pushes: temp numbers pushed in order
    def lift(il, v, ctx):
        for i, size in enumerate(pops):
            pop_temp(il, LLIL_TEMP(i), size)
        for i in pushes:
            push(il, pops[i], il.reg(pops[i], LLIL_TEMP(i)))
    return lift

def lift_binop(op, size, rsize=None, mask=None):
    rsize = rsize or size
    def lift(il, v, ctx):
        pop_temp(il, T1, rsize)  # value2
        pop_temp(il, T0, size)   # value1
        rhs = il.reg(rsize, T1)
        if mask is not None:
            rhs = il.and_expr(rsize, rhs, il.const(rsize, mask))
        push(il, size, getattr(il, op)(size, il.reg(size, T0), rhs))
    return lift

def lift_unop(op, size):
    return lambda il, v, ctx: push(il, size, getattr(il, op)(size, il.pop(size)))

def lift_fmod(size):
    def lift(il, v, ctx):
        pop_temp(il, T1, size)
        pop_temp(il, T0, size)
        il.append(il.intrinsic([T2], "fmod", [il.reg(size, T0), il.reg(size, T1)]))
        push(il, size, il.reg(size, T2))
    return lift

def lift_convert(src, dst, op):
    return lambda il, v, ctx: push(il, dst, getattr(il, op)(dst, il.pop(src)))

def lift_narrow(part, extend):
    return lambda il, v, ctx: push(il, 4, getattr(il, extend)(4, il.low_part(part, il.pop(4))))

def lift_iinc(il, v, ctx):
    index, const = v
    il.append(set_local(il, index, 4, il.add(4, local_expr(il, index, 4), il.const(4, const))))

def lift_lcmp(il, v, ctx):
    # (a > b) - (a < b)
    pop_temp(il, T1, 8)
    pop_temp(il, T0, 8)
    a, b = (lambda: il.reg(8, T0)), (lambda: il.reg(8, T1))
    push(il, 4, il.sub(4, il.bool_to_int(4, il.compare_signed_greater_than(8, a(), b())),
                          il.bool_to_int(4, il.compare_signed_less_than(8, a(), b()))))

def lift_fcmp(size, nan_result):
    def lift(il, v, ctx):
        pop_temp(il, T1, size)
        pop_temp(il, T0, size)
        a, b = (lambda: il.reg(size, T0)), (lambda: il.reg(size, T1))
        b2i = lambda cond: il.bool_to_int(4, cond)
        if nan_result < 0:
            # fcmpl: (a > b) + (a >= b) - 1   -> NaN gives -1
            result = il.sub(4, il.add(4, b2i(il.float_compare_greater_than(size, a(), b())),
                                         b2i(il.float_compare_greater_equal(size, a(), b()))), il.const(4, 1))
        else:
            # fcmpg: 1 - (a <= b) - (a < b)   -> NaN gives 1
            result = il.sub(4, il.sub(4, il.const(4, 1), b2i(il.float_compare_less_equal(size, a(), b()))),
                               b2i(il.float_compare_less_than(size, a(), b())))
        push(il, 4, result)
    return lift

def lift_if_zero(cmp):
    # pop into a temp first: a pop inside the if condition is not tracked by BN's stack analysis
    def lift(il, v, ctx):
        pop_temp(il, T0, 4)
        branch_if(il, getattr(il, cmp)(4, il.reg(4, T0), il.const(4, 0)), v)
    return lift

def lift_if_cmp(cmp, size=4):
    def lift(il, v, ctx):
        pop_temp(il, T1, size)
        pop_temp(il, T0, size)
        branch_if(il, getattr(il, cmp)(size, il.reg(size, T0), il.reg(size, T1)), v)
    return lift

def lift_goto(il, v, ctx):
    branch(il, v)

def lift_jsr(il, v, ctx):
    # subroutine modelled as a call that pops the pushed return address itself
    # a plain constant: as a const_pointer BN would start a bogus function at the return address
    push(il, ADDR_SIZE, il.const(ADDR_SIZE, ctx.addr+ctx.length))
    il.append(il.call_stack_adjust(il.const_pointer(ADDR_SIZE, v), ADDR_SIZE))

def lift_ret(il, v, ctx):
    il.append(il.ret(local_expr(il, v, ADDR_SIZE)))

def lift_switch(cases_index):
    def lift(il, v, ctx):
        pop_temp(il, T0, 4)
        for key, target in v[cases_index]:
            branch_if(il, il.compare_equal(4, il.reg(4, T0), il.const(4, key)), target)
        branch(il, v[0])
    return lift

def lift_return(size):
    def lift(il, v, ctx):
        if size == 4:
            il.append(il.set_reg(4, "r", il.pop(4)))
        elif size == 8:
            il.append(il.set_reg_split(4, "rh", "r", il.pop(8)))
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
    push(il, size, il.load(size, pool_pointer(il, v)))

def lift_putstatic(il, v, ctx):
    size = field_size(ctx, v)
    il.append(il.store(size, pool_pointer(il, v), il.pop(size)))

def lift_getfield(il, v, ctx):
    size = field_size(ctx, v)
    pop_temp(il, T0, ADDR_SIZE)
    il.append(il.intrinsic([T1], "getfield", [il.reg(ADDR_SIZE, T0), pool_pointer(il, v)]))
    push(il, size, il.reg(size, T1))

def lift_putfield(il, v, ctx):
    size = field_size(ctx, v)
    pop_temp(il, T1, size)
    pop_temp(il, T0, ADDR_SIZE)
    il.append(il.intrinsic([], "putfield", [il.reg(ADDR_SIZE, T0), pool_pointer(il, v), il.reg(size, T1)]))

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
        temps = [LLIL_TEMP(i) for i in range(len(sizes))]
        for i in reversed(range(len(sizes))):
            pop_temp(il, temps[i], sizes[i])
        params = [pool_pointer(il, index)] + [il.reg(sizes[i], temps[i]) for i in range(len(sizes))]
        if ret == 'V':
            il.append(il.intrinsic([], kind, params))
        else:
            out = LLIL_TEMP(len(sizes))
            il.append(il.intrinsic([out], kind, params))
            push(il, slot_size(ret), il.reg(slot_size(ret), out))
    return lift

def lift_new(il, v, ctx):
    il.append(il.intrinsic([T0], "new", [pool_pointer(il, v)]))
    push(il, ADDR_SIZE, il.reg(ADDR_SIZE, T0))

def lift_newarray(il, v, ctx):
    pop_temp(il, T0, 4)
    il.append(il.intrinsic([T1], "newarray", [il.const_pointer(ADDR_SIZE, PSEUDOMEMORY_PRIMITIVES+v), il.reg(4, T0)]))
    push(il, ADDR_SIZE, il.reg(ADDR_SIZE, T1))

def lift_anewarray(il, v, ctx):
    pop_temp(il, T0, 4)
    il.append(il.intrinsic([T1], "anewarray", [pool_pointer(il, v), il.reg(4, T0)]))
    push(il, ADDR_SIZE, il.reg(ADDR_SIZE, T1))

def lift_multianewarray(il, v, ctx):
    index, dims = v
    for i in reversed(range(dims)):
        pop_temp(il, LLIL_TEMP(i), 4)
    out = LLIL_TEMP(dims)
    il.append(il.intrinsic([out], "multianewarray", [pool_pointer(il, index)] + [il.reg(4, LLIL_TEMP(i)) for i in range(dims)]))
    push(il, ADDR_SIZE, il.reg(ADDR_SIZE, out))

def lift_object_op(name, with_class, size):
    # pops an objectref, optionally passes the class operand, pushes a result of `size` (0 = none)
    def lift(il, v, ctx):
        pop_temp(il, T0, ADDR_SIZE)
        params = [il.reg(ADDR_SIZE, T0)]
        if with_class:
            params.append(pool_pointer(il, v))
        if size == 0:
            il.append(il.intrinsic([], name, params))
        else:
            il.append(il.intrinsic([T1], name, params))
            push(il, size, il.reg(size, T1))
    return lift

def lift_athrow(il, v, ctx):
    pop_temp(il, T0, ADDR_SIZE)
    il.append(il.intrinsic([], "athrow", [il.reg(ADDR_SIZE, T0)]))
    il.append(il.no_ret())

def lift_wide(il, v, ctx):
    name = InstructionNames[v[0]]
    if name == "iinc":
        lift_iinc(il, (v[1], v[2]), ctx)
    else:
        InstructionIL[name](il, v[1], ctx)

INTRINSICS = ["invokevirtual", "invokespecial", "invokestatic", "invokeinterface", "invokedynamic",
              "getfield", "putfield", "new", "newarray", "anewarray", "multianewarray", "arraylength",
              "athrow", "checkcast", "instanceof", "monitorenter", "monitorexit", "fmod"]

InstructionIL = {
    "nop":         lambda il, v, ctx: il.append(il.nop()),
    "aconst_null": lambda il, v, ctx: push(il, ADDR_SIZE, il.const(ADDR_SIZE, 0)),
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
    "bipush":      lambda il, v, ctx: push(il, 4, il.const(4, v)),
    "sipush":      lambda il, v, ctx: push(il, 4, il.const(4, v)),
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

    # operand stack: every slot is 4 bytes, category-2 values (long/double) take two slots
    "pop":         lift_stack_shuffle([4], []),
    "pop2":        lift_stack_shuffle([8], []),
    "dup":         lift_stack_shuffle([4], [0, 0]),
    "dup_x1":      lift_stack_shuffle([4, 4], [0, 1, 0]),
    "dup_x2":      lift_stack_shuffle([4, 8], [0, 1, 0]),
    "dup2":        lift_stack_shuffle([8], [0, 0]),
    "dup2_x1":     lift_stack_shuffle([8, 4], [0, 1, 0]),
    "dup2_x2":     lift_stack_shuffle([8, 8], [0, 1, 0]),
    "swap":        lift_stack_shuffle([4, 4], [0, 1]),

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

