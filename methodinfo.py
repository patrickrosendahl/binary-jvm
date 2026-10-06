"""Per-method analysis shared by the architecture and the lifter (pure Python, no Binary Ninja import).

The lifter only ever sees one instruction at a time; everything that needs the whole method -- the
operand-stack shape at each instruction, branch targets, exception-table chains -- is computed once per
method here and cached on the class reader:

    mi = method_info(reader, addr)     # None outside a method with code
    mi.state(addr)                     # operand-stack categories (bottom..top) on entry, None if unreachable
"""
from .constants import METHOD_BASE, METHOD_STRIDE, NUM_STACK_REGS, method_address
from .opcodes import decode_instruction, CONDITIONAL_BRANCHES
from . import stackmap

COMPARES = {"lcmp", "fcmpl", "fcmpg", "dcmpl", "dcmpg"}
IF_ZERO = {"ifeq", "ifne", "iflt", "ifge", "ifgt", "ifle"}

class MethodInfo:
    def __init__(self, reader, method):
        self.index = method.index
        self.base = method_address(method.index)
        attr = method.code_attribute.attribute
        self.code = bytes(reader.data[attr.start_address:attr.end_address])
        self.exception_table = list(attr.exception_table)

        # linear decode: offset -> (name, operand type, length, value)
        self.decoded = {}
        off = 0
        while off < len(self.code):
            d = decode_instruction(self.code[off:], self.base + off)
            if d[0] is None:
                break
            self.decoded[off] = d
            off += d[2]

        # offsets control can arrive at other than by falling through
        self.handlers = {entry[2] for entry in self.exception_table}
        self.targets = set(self.handlers)
        for off, (name, _, length, value) in self.decoded.items():
            if name in ("goto", "goto_w", "jsr", "jsr_w") or name in CONDITIONAL_BRANCHES:
                self.targets.add(value - self.base)
                if name in ("jsr", "jsr_w"):
                    self.targets.add(off + length)  # where `ret` comes back
            elif name == "tableswitch":
                self.targets.update([value[0] - self.base] + [t - self.base for _, t in value[3]])
            elif name == "lookupswitch":
                self.targets.update([value[0] - self.base] + [t - self.base for _, t in value[2]])

        self.states, self.max_depth, self.errors = stackmap.compute_stack_states(
            self.code, self.base, self.exception_table, reader.memberDescriptor)

        # compare fusion (jvm-32): lcmp/fcmp*/dcmp* directly followed by if<cond> that nothing else jumps to
        # lifts as one comparison. fused_cmp: compare offsets; fused_if: if offset -> compare name
        self.fused_cmp, self.fused_if = set(), {}
        for off, (name, _, length, _) in self.decoded.items():
            nxt = off + length
            if (name in COMPARES and nxt in self.decoded and self.decoded[nxt][0] in IF_ZERO
                    and nxt not in self.targets and off in self.states and nxt in self.states
                    and len(self.states[off]) <= NUM_STACK_REGS):
                self.fused_cmp.add(off)
                self.fused_if[nxt] = name

    def state(self, addr):
        return self.states.get(addr - self.base)


def method_info(reader, addr):
    """MethodInfo of the method whose code contains addr (cached per reader), or None"""
    if reader is None or getattr(reader, "classStruct", None) is None or addr < METHOD_BASE:
        return None
    index = (addr - METHOD_BASE) // METHOD_STRIDE
    cache = reader.__dict__.get("_method_infos")
    if cache is None:
        cache = reader._method_infos = {}
    if index in cache:
        return cache[index]
    methods = reader.classStruct.methods
    mi = None
    if index < len(methods) and methods[index].code_attribute is not None:
        try:
            mi = MethodInfo(reader, methods[index])
        except Exception:
            mi = None
    cache[index] = mi
    return mi
