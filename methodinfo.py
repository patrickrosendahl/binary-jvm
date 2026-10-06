"""Per-method analysis shared by the architecture and the lifter (pure Python, no Binary Ninja import).

The lifter only ever sees one instruction at a time; everything that needs the whole method -- the
operand-stack shape at each instruction, branch targets, exception-table chains -- is computed once per
method here and cached on the class reader:

    mi = method_info(reader, addr)     # None outside a method with code
    mi.state(addr)                     # operand-stack categories (bottom..top) on entry, None if unreachable
"""
from .constants import METHOD_BASE, METHOD_STRIDE, NUM_STACK_REGS, method_address
from .opcodes import decode_instruction, CONDITIONAL_BRANCHES, RETURNS
from . import stackmap

COMPARES = {"lcmp", "fcmpl", "fcmpg", "dcmpl", "dcmpg"}
IF_ZERO = {"ifeq", "ifne", "iflt", "ifge", "ifgt", "ifle"}

# exception edges (jvm-42)
THROWERS = {"invokevirtual", "invokespecial", "invokestatic", "invokeinterface", "invokedynamic", "athrow"}
IMPLICIT_THROWERS = {"getfield", "putfield", "idiv", "ldiv", "irem", "lrem", "checkcast", "arraylength",
                     "monitorenter", "monitorexit", "iaload", "laload", "faload", "daload", "aaload", "baload",
                     "caload", "saload", "iastore", "lastore", "fastore", "dastore", "aastore", "bastore",
                     "castore", "sastore"}
# implicit throwers (NullPointerException, ArithmeticException, ...) only get an exception check when a
# handler of their try range could catch such an exception
BROAD_CATCH = {"", "java/lang/Throwable", "java/lang/Exception", "java/lang/RuntimeException"}
PROPAGATE = None  # "next handler" of the last typed handler of a chain: the exception leaves the method
_ENDS_BLOCK = stackmap.ENDS_FLOW | CONDITIONAL_BRANCHES | {"jsr", "jsr_w"}

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

        self._exception_edges(reader)

    def _chain(self, off):
        """exception-table entries covering off, as [(handler, (catch types...)), ...] in search order:
        consecutive entries for one handler (multi-catch) are merged, a catch-all ("") ends the chain,
        a handler that comes back later in the chain is dropped (its earlier group wins)"""
        chain, seen = [], set()
        for start, end, handler, catch in self.exception_table:
            if not (start <= off < end):
                continue
            name = self.class_names.get(catch, "") if catch else ""
            if chain and chain[-1][0] == handler:
                chain[-1] = (handler, chain[-1][1] + (name,))
            elif handler in seen:
                continue
            else:
                chain.append((handler, (name,)))
                seen.add(handler)
            if name == "":
                break
        return tuple(chain)

    def _exception_edges(self, reader):
        # catch types by pool index
        self.class_names, self.class_index = {}, {}
        for _, _, _, catch in self.exception_table:
            if catch and catch not in self.class_names:
                entry = reader.poolEntry(catch)
                self.class_names[catch] = name = str(entry) if entry is not None else "?#%d" % catch
                self.class_index.setdefault(name, catch)
        # throw sites: offset -> chain of the first handler to try
        self.throw_sites = {}
        if self.exception_table:
            implicit_done = False  # at most one implicit-thrower check per basic block
            for off in sorted(self.decoded):
                name = self.decoded[off][0]
                if name == "wide":
                    name = None
                if off in self.targets:
                    implicit_done = False
                if off in self.states and (name in THROWERS or name in IMPLICIT_THROWERS):
                    chain = self._chain(off)
                    if chain and name in THROWERS:
                        self.throw_sites[off] = chain
                    elif chain and not implicit_done and any(t in BROAD_CATCH for _, types in chain for t in types):
                        self.throw_sites[off] = chain
                        implicit_done = True
                if name in _ENDS_BLOCK:
                    implicit_done = False
        # what each handler does on entry: handler -> (catch types, or () for catch-all; next handler or
        # PROPAGATE). A handler reached through chains that disagree on it is a conflict: its entry
        # dispatches through an indirect branch to every possible next handler.
        self.dispatch, self.conflicts = {}, {}
        for chain in set(self.throw_sites.values()):
            for i, (handler, types) in enumerate(chain):
                if "" in types:
                    spec = ((), PROPAGATE)
                else:
                    spec = (types, chain[i + 1][0] if i + 1 < len(chain) else PROPAGATE)
                old = self.dispatch.setdefault(handler, spec)
                if old != spec:
                    self.conflicts.setdefault(handler, {old}).add(spec)

    def branches(self, off):
        """extra (BN branch type name, absolute target) edges for the instruction at off from the exception
        model: the handler-entry type test and the throw-site check. Empty for most instructions."""
        extra = []
        if off in self.conflicts:
            extra.append(("IndirectBranch", None))
        else:
            spec = self.dispatch.get(off)
            if spec is not None and spec[0] and spec[1] is not PROPAGATE:
                extra.append(("TrueBranch", self.base + spec[1]))
        chain = self.throw_sites.get(off)
        if chain is not None:
            if self.decoded[off][0] == "athrow":
                extra.append(("UnconditionalBranch", self.base + chain[0][0]))
            else:
                extra.append(("TrueBranch", self.base + chain[0][0]))
        return extra

    def conflict_targets(self, handler):
        """absolute addresses an indirect handler-entry dispatch can go to"""
        return sorted({self.base + nxt for _, nxt in self.conflicts.get(handler, ()) if nxt is not PROPAGATE})

    def state(self, addr):
        return self.states.get(addr - self.base)


def instruction_branches(name, value, addr, length, mi=None):
    """the instruction's branches for InstructionInfo as [(BranchType name, target)] (at most 3).
    mi: its MethodInfo, for the exception edges (None: plain control flow only)"""
    extra = mi.branches(addr - mi.base) if mi is not None else []
    if name == "athrow":
        # caught in this method: a jump to the handler (UnconditionalBranch in extra); else it leaves
        if all(kind != "UnconditionalBranch" for kind, _ in extra):
            extra = extra + [("ExceptionBranch", 0)]
        return [(kind, target or 0) for kind, target in extra]
    if name in ("goto", "goto_w"):
        result = [("UnconditionalBranch", value)]
    elif name in ("jsr", "jsr_w"):
        result = [("CallDestination", value)]
    elif name in ("tableswitch", "lookupswitch"):
        result = [("IndirectBranch", 0)]  # targets come from the lifted compare chain (and analyze_tables)
    elif name in RETURNS:
        result = [("FunctionReturn", 0)]
    elif name in CONDITIONAL_BRANCHES:
        result = [("TrueBranch", value), ("FalseBranch", addr + length)]
    else:
        result = []
    if extra:
        # exception edges: the handler-entry type test and/or the check after a throw site
        fallthrough = not result
        result += [(kind, target or 0) for kind, target in extra]
        if fallthrough:
            result.append(("FalseBranch", addr + length))
    return result


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
