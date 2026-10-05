"""Operand-stack shape per instruction (pure Python, no Binary Ninja import).

The JVM verifier guarantees that at every instruction the operand stack has the same shape on every
path. This module recomputes that shape -- the category of every stack entry, bottom to top:
1 = one 4-byte slot (int/float/reference/returnAddress), 2 = an 8-byte long/double (two JVM slots,
one entry) -- with a worklist dataflow over the method's control flow, so the lifter can tell e.g.
which `dup2` form applies or which stack slots hold 8-byte values.

    states, max_depth, errors = compute_stack_states(code, base, exception_table, descriptor_of)

states: {code offset: tuple of categories on entry}; unreachable instructions have no entry.
"""
from .opcodes import InstructionNames, decode_instruction, parse_method_descriptor, CONDITIONAL_BRANCHES

def _cat(descriptor_char):
    return 2 if descriptor_char in "JD" else 1

# fixed effects: name -> (categories popped, top of stack first; categories pushed, bottom first)
_FIXED = {}
def _fx(names, pops, pushes):
    for name in names.split():
        _FIXED[name] = (tuple(pops), tuple(pushes))

_fx("nop iinc goto goto_w", [], [])
_fx("aconst_null iconst_m1 iconst_0 iconst_1 iconst_2 iconst_3 iconst_4 iconst_5 fconst_0 fconst_1 fconst_2 bipush sipush ldc ldc_w new", [], [1])
_fx("lconst_0 lconst_1 dconst_0 dconst_1 ldc2_w", [], [2])
for _k, _c in (("i", 1), ("f", 1), ("a", 1), ("l", 2), ("d", 2)):
    _fx(" ".join([_k+"load"] + ["%sload_%d" % (_k, _i) for _i in range(4)]), [], [_c])
    _fx(" ".join([_k+"store"] + ["%sstore_%d" % (_k, _i) for _i in range(4)]), [_c], [])
    _fx(_k+"aload", [1, 1], [_c])            # index, arrayref
    _fx(_k+"astore", [_c, 1, 1], [])         # value, index, arrayref
_fx("baload caload saload", [1, 1], [1])
_fx("bastore castore sastore", [1, 1, 1], [])
_fx("iadd isub imul idiv irem ishl ishr iushr iand ior ixor fadd fsub fmul fdiv frem fcmpl fcmpg", [1, 1], [1])
_fx("ladd lsub lmul ldiv lrem land lor lxor dadd dsub dmul ddiv drem", [2, 2], [2])
_fx("lshl lshr lushr", [1, 2], [2])         # int shift count on top of the long
_fx("lcmp dcmpl dcmpg", [2, 2], [1])
_fx("ineg fneg i2f f2i i2b i2c i2s", [1], [1])
_fx("lneg dneg l2d d2l", [2], [2])
_fx("i2l i2d f2l f2d", [1], [2])
_fx("l2i l2f d2i d2f", [2], [1])
_fx("ifeq ifne iflt ifge ifgt ifle ifnull ifnonnull tableswitch lookupswitch monitorenter monitorexit", [1], [])
_fx("if_icmpeq if_icmpne if_icmplt if_icmpge if_icmpgt if_icmple if_acmpeq if_acmpne", [1, 1], [])
_fx("ireturn freturn areturn athrow", [1], [])
_fx("lreturn dreturn", [2], [])
_fx("return ret", [], [])
_fx("newarray anewarray arraylength checkcast instanceof", [1], [1])
_fx("swap", [1, 1], [])                      # pushes handled specially

# stack manipulation in JVM slots: (slots duplicated from the top, slots they are inserted below)
_DUP = {"dup": (1, 0), "dup_x1": (1, 1), "dup_x2": (1, 2), "dup2": (2, 0), "dup2_x1": (2, 1), "dup2_x2": (2, 2)}
_POP = {"pop": 1, "pop2": 2}

ENDS_FLOW = {"ireturn", "lreturn", "freturn", "dreturn", "areturn", "return", "athrow", "ret",
             "goto", "goto_w", "tableswitch", "lookupswitch"}

class StackError(Exception):
    pass

def _take_slots(stack, slots):
    """split off top entries covering exactly `slots` JVM slots -> (rest, taken)"""
    i, n = len(stack), 0
    while n < slots:
        if i == 0:
            raise StackError("stack underflow")
        i -= 1
        n += stack[i]
    if n != slots:
        raise StackError("%d-slot operation splits a category-2 value" % slots)
    return stack[:i], stack[i:]

def _pop(stack, cats):
    for c in cats:
        if not stack:
            raise StackError("stack underflow")
        if stack[-1] != c:
            raise StackError("expected category %d on top, found %d" % (c, stack[-1]))
        stack = stack[:-1]
    return stack

def _effect(name, value, stack, descriptor_of):
    """stack (tuple) on entry -> stack after the instruction"""
    if name == "wide":
        return _effect(InstructionNames[value[0]], value[1], stack, descriptor_of)
    if name in _DUP:
        n, m = _DUP[name]
        rest, top = _take_slots(stack, n)
        rest, below = _take_slots(rest, m)
        return rest + top + below + top
    if name in _POP:
        return _take_slots(stack, _POP[name])[0]
    if name == "swap":
        rest = _pop(stack, (1, 1))
        return rest + (1, 1)
    if name in ("jsr", "jsr_w"):
        return stack + (1,)                  # returnAddress, seen at the subroutine entry
    if name == "multianewarray":
        return _pop(stack, (1,) * value[1]) + (1,)
    if name in _FIXED:
        pops, pushes = _FIXED[name]
        return _pop(stack, pops) + pushes
    index = value[0] if isinstance(value, tuple) else value
    desc = descriptor_of(index)
    if not desc:
        raise StackError("no descriptor for pool entry %d" % index)
    if name in ("getstatic", "putstatic", "getfield", "putfield"):
        c = _cat(desc[0])
        return {"getstatic": lambda: stack + (c,),
                "putstatic": lambda: _pop(stack, (c,)),
                "getfield":  lambda: _pop(stack, (1,)) + (c,),
                "putfield":  lambda: _pop(stack, (c, 1))}[name]()
    if name.startswith("invoke"):
        args, ret = parse_method_descriptor(desc)
        pops = tuple(_cat(a) for a in reversed(args))
        if name not in ("invokestatic", "invokedynamic"):
            pops += (1,)                     # objectref
        stack = _pop(stack, pops)
        return stack if ret == 'V' else stack + (_cat(ret),)
    raise StackError("no stack effect for %s" % name)

def _successors(name, value, off, length, base):
    """code offsets control can reach next (excluding exception handlers)"""
    if name in ("goto", "goto_w"):
        return [value - base]
    if name in CONDITIONAL_BRANCHES:
        return [value - base, off + length]
    if name == "tableswitch":
        return [value[0] - base] + [t - base for _, t in value[3]]
    if name == "lookupswitch":
        return [value[0] - base] + [t - base for _, t in value[2]]
    if name in ENDS_FLOW:
        return []
    return [off + length]

def compute_stack_states(code, base, exception_table, descriptor_of):
    """code: the method's bytecode; base: absolute address it is mapped at (switch padding is
    relative to it, so it must be 4-aligned); exception_table: [(start_pc, end_pc, handler_pc,
    catch_type)]; descriptor_of(pool_index) -> descriptor string or None.
    Returns (states {offset: categories bottom..top}, max depth in JVM slots, errors [str])."""
    states, errors = {}, []
    max_depth = 0
    decoded = {}
    work = [0]
    states[0] = ()

    def merge(target, stack):
        if target < 0 or target >= len(code):
            errors.append("+%x: branch to +%x outside the code" % (off, target))
            return
        old = states.get(target)
        if old is None:
            states[target] = stack
            work.append(target)
        elif old != stack:
            errors.append("+%x: stack merge conflict %r vs %r" % (target, old, stack))

    while work:
        off = work.pop()
        stack = states[off]
        max_depth = max(max_depth, sum(stack))
        if off not in decoded:
            decoded[off] = decode_instruction(code[off:], base + off)
        name, _, length, value = decoded[off]
        if name is None:
            errors.append("+%x: undecodable instruction" % off)
            continue
        for start, end, handler, _ in exception_table:
            if start <= off < end:
                merge(handler, (1,))
        try:
            after = _effect(name, value, stack, descriptor_of)
        except StackError as e:
            errors.append("+%x %s: %s (stack %r)" % (off, name, e, stack))
            continue
        max_depth = max(max_depth, sum(after))
        if name in ("jsr", "jsr_w"):
            merge(value - base, after)       # subroutine entry sees the return address
            merge(off + length, stack)       # its `ret` comes back here with the old stack
            continue
        for target in _successors(name, value, off, length, base):
            merge(target, after)
    return states, max_depth, errors
