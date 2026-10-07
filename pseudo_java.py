# Copyright (c) 2024-2026 Vector 35 Inc
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.
#
# Adapted for binary-jvm from Vector35's example
# `examples/python/pseudo_python.py` (Binary Ninja 6.1): the HLIL walk, precedence handling and
# token-emitter usage come from there; the syntax is Java's and the JVM-specific parts (pool-slot calls,
# object intrinsics, javac idioms, try/catch from the exception table) are new.
"""Pseudo Java: a language representation that prints JVM-method HLIL as Java-like source.

What it does on top of plain HLIL (all at render time, nothing is rewritten in the IL):
  * calls through constant-pool slots print as `obj.m(x)` (receiver = first argument) or `Foo.m(x)`
    (static: the call has exactly the descriptor's arguments)
  * object intrinsics: `obj.f`, `obj.f = v`, `new T[n]`, `(T) x`, `x instanceof T`, `a.length`, `throw x`,
    `// synchronized (o) {` markers; static fields `Foo.f`; pool strings as Java string literals
  * idioms (jvm-44): `v = new(X); X.<init>(v, a)` -> `X v = new X(a)`, StringBuilder / StringBuffer
    append chains and invokedynamic makeConcatWithConstants -> `a + b`, boxing `Integer.valueOf(x)` /
    `x.intValue()` -> `x` (HIDE_BOXING), `<init>`/`<clinit>` -> constructor / `static {}`, `super(...)`
  * try/catch from the method's exception table (`jvm.exception_table` function metadata, else the
    class file), folded to Java's statements by group_try_entries (catch + finally of one try, split
    ranges). Runs of statements inside a try range get wrapped in `try { } catch (T e) { } finally { }`.
    The handler code lives inside the method (jvm-42): the catch body is the HLIL from the handler pc
    (an `if (exc != 0) { ... }` branch or a labelled block), `e` is the variable the handler stores exc
    in; a finally body is the handler's code up to its rethrow, or the cleanup of the inlined
    `cleanup; __propagate(exc)` rethrow paths.
  * the lifter's exception plumbing (`exc = __exception()`, `if (exc != 0)` / `if (exc == 0)` tests --
    their normal path continues in line --, handler type tests, rethrow paths, synchronized cleanup
    handlers) is not printed; `exc = v` (athrow in a try) and `__propagate(v)` print as `throw v`.
    An exceptional branch that cannot be attributed to a printed handler stays as a commented block,
    and a statement that fails to render is printed as its HLIL in comments: nothing is dropped.

`render_method(func) -> list[str]` returns the Java header plus body lines (used by the class view).
"""
from .constants import (ARCH_NAME, VIEW_NAME, PSEUDOMEMORY_TABLE, PSEUDOMEMORY_PRIMITIVES, POOL_STRIDE,
                        METHOD_BASE, METHOD_STRIDE)

# ---------------------------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------------------------
HIDE_BOXING = True          # Integer.valueOf(x) / x.intValue() -> x
FOLD_TEMPORARIES = True      # print a single-use operand-stack temporary inside its use (jvm-46)
SYNC_BLOCKS = True           # monitorenter ... monitorexit -> synchronized (x) { ... } (jvm-47)
# intrinsics without side effects (reordering them past a call is still not allowed: they read memory)
PURE_INTRINSICS = {"getfield", "arraylength", "checkcast", "instanceof", "new", "newarray", "anewarray",
                   "multianewarray", "__exception"}
QUALIFY_OWN_STATICS = False  # print `m(x)` instead of `ThisClass.m(x)` for static calls into the own class
SIMPLE_CLASS_NAMES = True    # java.lang.String -> String


def language_name_for(arch_name):
    """'JVM' -> 'Pseudo Java'; dev loads ('JVM-dev3') get their own name, since BN can't unregister one."""
    if arch_name == "JVM":
        return "Pseudo Java"
    return "Pseudo Java " + arch_name


LANGUAGE_NAME = language_name_for(ARCH_NAME)

# ---------------------------------------------------------------------------------------------------
# pure helpers (no Binary Ninja; exercised by tests/pseudo_java_check.py)
# ---------------------------------------------------------------------------------------------------
PRIMITIVES = {'B': 'byte', 'C': 'char', 'D': 'double', 'F': 'float', 'I': 'int', 'J': 'long', 'S': 'short',
              'Z': 'boolean', 'V': 'void'}
TYPE_CODES = {v: k for k, v in PRIMITIVES.items()}  # 'int' -> 'I'
NEWARRAY_TYPES = {4: 'boolean', 5: 'char', 6: 'float', 7: 'double', 8: 'byte', 9: 'short', 10: 'int', 11: 'long'}
BOXES = {"java/lang/Integer": ("intValue", "I"), "java/lang/Long": ("longValue", "J"),
         "java/lang/Short": ("shortValue", "S"), "java/lang/Byte": ("byteValue", "B"),
         "java/lang/Character": ("charValue", "C"), "java/lang/Boolean": ("booleanValue", "Z"),
         "java/lang/Float": ("floatValue", "F"), "java/lang/Double": ("doubleValue", "D")}
STRING_BUILDERS = ("java/lang/StringBuilder", "java/lang/StringBuffer")

ACC_PUBLIC, ACC_PRIVATE, ACC_PROTECTED, ACC_STATIC = 0x0001, 0x0002, 0x0004, 0x0008
ACC_FINAL, ACC_SYNCHRONIZED, ACC_NATIVE, ACC_ABSTRACT, ACC_VARARGS = 0x0010, 0x0020, 0x0100, 0x0400, 0x0080
ACC_STRICT = 0x0800


def java_class_name(internal, simple=None):
    """'java/lang/String' -> 'String' (or 'java.lang.String'); 'a/B$C' -> 'B.C'; '[I' -> 'int[]'."""
    if simple is None:
        simple = SIMPLE_CLASS_NAMES
    if internal.startswith('['):
        return field_type_name(internal, simple)
    name = internal.replace('/', '.')
    if simple:
        name = name.rsplit('.', 1)[-1]
    # inner classes: Outer$Inner -> Outer.Inner (anonymous Outer$1 stays as is)
    parts = name.split('$')
    if len(parts) > 1 and all(p and not p[0].isdigit() for p in parts[1:]):
        name = '.'.join(parts)
    return name


def parse_field_type(desc, i=0, simple=None):
    """Parse one field descriptor at desc[i]: returns (java type name, index after it)."""
    dims = 0
    while desc[i] == '[':
        dims += 1
        i += 1
    if desc[i] == 'L':
        end = desc.index(';', i)
        name = java_class_name(desc[i+1:end], simple)
        i = end + 1
    else:
        name = PRIMITIVES.get(desc[i], desc[i])
        i += 1
    return name + "[]" * dims, i


def field_type_name(desc, simple=None):
    return parse_field_type(desc, 0, simple)[0]


def descriptor_types(desc, simple=None):
    """'(I[JLjava/lang/String;)V' -> (['int', 'long[]', 'String'], 'void')"""
    args = []
    i = desc.index('(') + 1
    while desc[i] != ')':
        t, i = parse_field_type(desc, i, simple)
        args.append(t)
    ret, _ = parse_field_type(desc, i + 1, simple)
    return args, ret


def descriptor_arg_codes(desc):
    """'(ILjava/lang/String;[J)V' -> ['I', 'L', '['] (first character of each argument)"""
    codes = []
    i = desc.index('(') + 1
    while desc[i] != ')':
        start = i
        while desc[i] == '[':
            i += 1
        i = desc.index(';', i) + 1 if desc[i] == 'L' else i + 1
        codes.append(desc[start])
    return codes


def method_modifiers(flags):
    mods = []
    for bit, word in ((ACC_PUBLIC, "public"), (ACC_PRIVATE, "private"), (ACC_PROTECTED, "protected"),
                      (ACC_ABSTRACT, "abstract"), (ACC_STATIC, "static"), (ACC_FINAL, "final"),
                      (ACC_SYNCHRONIZED, "synchronized"), (ACC_NATIVE, "native"), (ACC_STRICT, "strictfp")):
        if flags & bit:
            mods.append(word)
    return mods


def method_header(class_name, name, descriptor, access_flags, param_names=None):
    """Java declaration of a method: 'public static void main(String[] args)', 'public Foo(int p1)',
    'static' for <clinit>. class_name is the internal name of the declaring class."""
    if name == "<clinit>":
        return "static"
    args, ret = descriptor_types(descriptor)
    names = list(param_names or [])
    names += ["p%d" % (i+1) for i in range(len(names), len(args))]
    if access_flags & ACC_VARARGS and args and args[-1].endswith("[]"):
        args[-1] = args[-1][:-2] + "..."
    params = ", ".join("%s %s" % (t, n) for t, n in zip(args, names))
    mods = method_modifiers(access_flags)
    if name == "<init>":
        mods = [m for m in mods if m not in ("static", "final", "synchronized", "abstract", "native")]
        return " ".join(mods + ["%s(%s)" % (java_class_name(class_name, True), params)])
    return " ".join(mods + [ret, "%s(%s)" % (name, params)])


def java_var_name(name):
    """BN variable names that are not Java identifiers ('com/x/Foo.LOGGER_1') -> 'LOGGER_1'"""
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    out = "".join(ch if ch.isalnum() or ch in "_$" else "_" for ch in name)
    return out if out and not out[0].isdigit() else "_" + out


def java_int_literal(value, size):
    """signed decimal for small values, hex otherwise; longs get an L suffix"""
    bits = size * 8 if size in (1, 2, 4, 8) else 32
    value &= (1 << bits) - 1
    if value >= 1 << (bits - 1):
        value -= 1 << bits
    text = str(value) if -0x10000 < value < 0x10000 else ("-%#x" % -value if value < 0 else "%#x" % value)
    return text + ("L" if size == 8 else "")


def arg_slots(desc, is_static):
    """local slot -> (argument index, descriptor code); long/double take two slots, `this` is slot 0"""
    out = {}
    slot = 0 if is_static else 1
    for i, code in enumerate(descriptor_arg_codes(desc)):
        out[slot] = (i, code)
        slot += 2 if code in "JD" else 1
    return out


def java_string_literal(s, quote='"'):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == quote or ch == '\\':
            out.append('\\' + ch)
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\t':
            out.append('\\t')
        elif ch == '\r':
            out.append('\\r')
        elif o < 0x20 or 0x7f <= o < 0xa0:
            out.append('\\u%04x' % o)
        else:
            out.append(ch)
    return quote + "".join(out) + quote


def concat_recipe_parts(recipe, args, constants=()):
    """invokedynamic makeConcatWithConstants: recipe '\\x01 = \\x01' with args [a, b] ->
    [('arg', a), ('lit', ' = '), ('arg', b)]; \\x02 takes the next bootstrap constant."""
    parts, lit, ai, ci = [], [], 0, 0
    for ch in recipe:
        if ch in '\x01\x02':
            if lit:
                parts.append(('lit', "".join(lit)))
                lit = []
            if ch == '\x01':
                parts.append(('arg', args[ai] if ai < len(args) else None))
                ai += 1
            else:
                parts.append(('lit', str(constants[ci]) if ci < len(constants) else ""))
                ci += 1
        else:
            lit.append(ch)
    if lit:
        parts.append(('lit', "".join(lit)))
    return parts


def group_try_entries(table):
    """Exception-table entries [[start, end, handler, type], ...] -> [(start, end, [(handler, type), ...])],
    one entry per Java try statement, sorted by start (outer first on ties).

    javac's try/catch/finally shapes are folded back into one statement:
      * a catch-all handler F (finally) protects the try range, every catch body (a range starting exactly at
        the catch handler) and its own store/rethrow code (a range starting at F); only the ranges before the
        first handler are the try body. Catches with the same first start whose body F covers (or with the
        same range) become the catch clauses of that try: try { } catch (T e) { } finally { }
      * a range split by inlined finally code (return inside try) is one region from the first start to
        the last end
      * typed handlers sharing the same ranges are the clauses of one try; a catch-all without a range before
        its own code (javac's self-covering entry) is not a try of its own.
    """
    by_handler = {}  # handler pc -> [type, [(start, end), ...]]
    order = []
    for start, end, handler, ctype in table:
        if handler not in by_handler:
            by_handler[handler] = [ctype, []]
            order.append(handler)
        by_handler[handler][1].append((start, end))
    groups = []
    used = set()
    for f in order:
        ctype, ranges = by_handler[f]
        if ctype:
            continue
        used.add(f)
        prim = [r for r in ranges if r[0] < f]
        if not prim:
            continue
        s0 = min(r[0] for r in prim)
        f_starts = {r[0] for r in ranges}
        catches = []
        for h in order:
            t, hr = by_handler[h]
            if not t or h in used or h > f or min(r[0] for r in hr) != s0:
                continue
            if h in f_starts or sorted(hr) == sorted(prim):
                catches.append(h)
        limit = min(catches + [f])
        prim = [r for r in prim if r[0] < limit] or prim
        used.update(catches)
        groups.append((s0, max(r[1] for r in prim), [(h, by_handler[h][0]) for h in catches] + [(f, "")]))
    rest = {}
    for h in order:
        if h in used:
            continue
        t, hr = by_handler[h]
        rest.setdefault(tuple(sorted(hr)), []).append((h, t))
    for hr, handlers in rest.items():
        groups.append((min(r[0] for r in hr), max(r[1] for r in hr), handlers))
    groups.sort(key=lambda g: (g[0], -g[1]))
    return groups


def try_runs(stmt_pcs, groups, active=()):
    """Place try ranges on one block's statements.

    stmt_pcs: per statement the list of pcs it covers (its own pc first), or None if unknown/neutral
    (labels, exception checks): those join a run between two inside statements but never start or end one.
    groups: from group_try_entries. Returns [(first, last, group)] for maximal runs of consecutive
    statements whose pcs all lie in the group's range and none of which is a handler of that group,
    largest ranges first, non-overlapping; groups in `active` (already open in an enclosing block) are skipped.
    """
    runs = []
    taken = set()
    for g in sorted(groups, key=lambda g: g[0] - g[1]):  # widest range first
        start, end, handlers = g
        if (start, end) in active:
            continue
        handler_pcs = {h for h, _ in handlers}
        inside = []  # True / False / None (neutral)
        for pcs in stmt_pcs:
            inside.append(None if not pcs else
                          all(start <= pc < end for pc in pcs) and pcs[0] not in handler_pcs)
        i = 0
        while i < len(inside):
            if inside[i] is not True:
                i += 1
                continue
            j = i
            k = i + 1
            while k < len(inside) and inside[k] is not False:
                if inside[k]:
                    j = k
                k += 1
            if not any(k in taken for k in range(i, j+1)):
                runs.append((i, j, g))
                taken.update(range(i, j+1))
            break  # one run per group: a try range is one region
        # (a second run of the same group would duplicate the catch clause)
    return sorted(runs, key=lambda r: r[0])


# ---------------------------------------------------------------------------------------------------
# Binary Ninja part
# ---------------------------------------------------------------------------------------------------
try:
    from binaryninja import (Architecture, BraceRequirement, DisassemblySettings, DisassemblyTextLine, Function,
                             InstructionTextToken, InstructionTextTokenType, LanguageRepresentationFunction,
                             LanguageRepresentationFunctionType, HighLevelILInstruction, HighLevelILFunction,
                             HighLevelILTokenEmitter, HighLevelILOperation, OperatorPrecedence, ScopeType,
                             SymbolDisplayType, SymbolDisplayResult, SymbolType, BoolType, VoidType, PointerType,
                             NamedTypeReferenceType, StructureType, InstructionTextTokenContext, StructureMember,
                             BinaryView, IntegerType, FloatType, VariableSourceType)
    from typing import Optional
    _HAVE_BN = isinstance(HighLevelILOperation.HLIL_NOP.value, int)  # False under the offline-check stub
except Exception:  # pragma: no cover
    _HAVE_BN = False

if _HAVE_BN:
    Op = HighLevelILOperation
    TT = InstructionTextTokenType
    P = OperatorPrecedence

    COMPOUND = None  # filled below

    def _tok(kind, text, **kw):
        return InstructionTextToken(kind, text, **kw)

    def _children(instr):
        for o in instr.operands:
            if isinstance(o, HighLevelILInstruction):
                yield o
            elif isinstance(o, list):
                for x in o:
                    if isinstance(x, HighLevelILInstruction):
                        yield x

    def _walk(instr):
        stack = [instr]
        while stack:
            i = stack.pop()
            yield i
            stack.extend(reversed(list(_children(i))))

    def _child_blocks(instr):
        """the outermost HLIL_BLOCKs below instr (not below those)"""
        stack = list(reversed(list(_children(instr))))
        while stack:
            i = stack.pop()
            if i.operation == Op.HLIL_BLOCK:
                yield i
                continue
            stack.extend(reversed(list(_children(i))))

    def _stmts_preorder(block):
        """(block, index, statement) for every statement of every block below `block`, in source order"""
        if block.operation != Op.HLIL_BLOCK:
            yield block, 0, block
            for b in _child_blocks(block):
                yield from _stmts_preorder(b)
            return
        for idx, s in enumerate(block.body):
            yield block, idx, s
            for b in ([s] if s.operation == Op.HLIL_BLOCK else _child_blocks(s)):
                yield from _stmts_preorder(b)

    _EXC_BRANCH = "EXC_BRANCH"

    class _ExcBranch:
        """the exceptional branch of an `if (exc ...)` test, as an item of a flattened statement list"""
        operation = _EXC_BRANCH
        operands = ()

        def __init__(self, instr, branch, kind):
            self.instr, self.branch, self.kind = instr, branch, kind
            self.address = instr.address
            self.expr_index = instr.expr_index

    class _StmtList:
        """statements following an exception test that only run on an exception, as a pseudo block"""
        operation = Op.HLIL_BLOCK
        as_ast = True

        def __init__(self, stmts):
            self.body = list(stmts)
            self.address = stmts[0].address
            self.expr_index = -1 - stmts[0].expr_index  # never a real block

        @property
        def operands(self):
            return [self.body]

    def _const_target(instr):
        """address a pool reference expression points at (CONST_PTR / IMPORT / CONST), else None"""
        if instr.operation in (Op.HLIL_CONST_PTR, Op.HLIL_IMPORT, Op.HLIL_EXTERN_PTR):
            return instr.constant
        if instr.operation == Op.HLIL_CONST and instr.constant >= PSEUDOMEMORY_TABLE:
            return instr.constant
        return None

    def _pool_index(addr):
        if addr is None or not (PSEUDOMEMORY_TABLE <= addr < PSEUDOMEMORY_TABLE + 0x10000 * POOL_STRIDE):
            return None
        if (addr - PSEUDOMEMORY_TABLE) % POOL_STRIDE:
            return None
        return (addr - PSEUDOMEMORY_TABLE) // POOL_STRIDE

    def java_type_of(t, simple=None):
        """BN type -> Java type name ('int', 'String', 'Object', 'long', ...)"""
        if t is None:
            return "var"
        try:
            text = str(t).replace("const ", "").strip()
            if text in JNI_TYPEDEFS:  # the view's 4-byte boolean/byte/char/short typedefs
                return JNI_TYPEDEFS[text]
            if isinstance(t, VoidType):
                return "void"
            if isinstance(t, BoolType):
                return "boolean"
            if isinstance(t, FloatType):
                return "float" if t.width == 4 else "double"
            if isinstance(t, IntegerType):
                return {1: "byte", 2: "char" if not t.signed else "short", 4: "int", 8: "long"}.get(t.width, "int")
            if isinstance(t, PointerType):
                target = t.target
                if isinstance(target, VoidType):
                    return "Object"
                if isinstance(target, (NamedTypeReferenceType, StructureType)):
                    name = str(target.name if isinstance(target, NamedTypeReferenceType) else
                               (target.registered_name.name if target.registered_name else target))
                    return _dotted_to_java(name, simple)
                if isinstance(target, PointerType):
                    return java_type_of(target, simple) + "[]"
                return java_type_of(target, simple) + "[]"
            if isinstance(t, (NamedTypeReferenceType, StructureType)):
                return _dotted_to_java(str(t.name if isinstance(t, NamedTypeReferenceType) else t), simple)
        except Exception:
            pass
        return str(t)

    JNI_TYPEDEFS = {"jboolean": "boolean", "jbyte": "byte", "jchar": "char", "jshort": "short",
                    "jint": "int", "jlong": "long", "jfloat": "float", "jdouble": "double"}

    def _dotted_to_java(name, simple=None):
        """the view's class struct names are dotted binary names ('java.lang.String', 'a.Outer$5')"""
        name = name.replace("struct ", "").strip()
        if name in JNI_TYPEDEFS:
            return JNI_TYPEDEFS[name]
        return java_class_name(name.replace(".", "/"), simple)

    class _ClassInfo:
        """Per-view lookups: pool entries, method infos, class name (cached per view handle)."""
        _cache = {}

        @classmethod
        def get(cls, view):
            try:
                from .classfile import view_key
                key = view_key(view)
            except Exception:
                key = id(view)
            info = cls._cache.get(key)
            if info is None:
                info = cls._cache[key] = cls(view)
            return info

        def __init__(self, view):
            self.view = view
            self.reader = None
            try:
                from .classfile import reader_for_view
                self.reader = reader_for_view(view)
            except Exception:
                pass
            self.meta = None
            try:
                self.meta = view.query_metadata("jvm.class")
            except Exception:
                pass
            self.class_name = self._class_name()

        def _class_name(self):
            if isinstance(self.meta, dict) and self.meta.get("name"):
                return str(self.meta["name"]).replace(".", "/")
            r = self.reader
            try:
                return str(r.poolEntry(r.classStruct.this_class))
            except Exception:
                return None

        def entry(self, index):
            try:
                return self.reader.poolEntry(index) if self.reader is not None else None
            except Exception:
                return None

        def kind(self, index):
            e = self.entry(index)
            return type(e).__name__ if e is not None else None

        def member(self, index):
            """(owner internal name, member name, descriptor) of a Field/Method/InterfaceMethod ref or indy"""
            e = self.entry(index)
            if e is None:
                sym = self.view.get_symbol_at(PSEUDOMEMORY_TABLE + index * POOL_STRIDE)
                if sym is None:
                    return None
                full = sym.full_name if "." in sym.full_name else sym.name
                owner, _, name = full.rpartition(".")
                return owner, name, None
            name = type(e).__name__
            try:
                if name == "JVMInvokeDynamic":
                    return None, str(self.reader.poolEntry(e.nat)), self.reader.memberDescriptor(index)
                owner = str(self.reader.poolEntry(e.classReference))
                return owner, str(self.reader.poolEntry(e.nameAndType)), self.reader.memberDescriptor(index)
            except Exception:
                return None

        def class_ref(self, index):
            e = self.entry(index)
            if type(e).__name__ == "JVMClassReference":
                return str(e)
            sym = self.view.get_symbol_at(PSEUDOMEMORY_TABLE + index * POOL_STRIDE)
            return sym.name if sym is not None else None

        def string(self, index):
            e = self.entry(index)
            if type(e).__name__ == "JVMStringReference":
                try:
                    return str(self.reader.poolEntry(e.index))
                except Exception:
                    return None
            return None

        def indy_recipe(self, index):
            """(recipe, constants) of a makeConcatWithConstants invokedynamic, else None"""
            e = self.entry(index)
            if type(e).__name__ != "JVMInvokeDynamic":
                return None
            try:
                tpl = self.reader.getBootstrap(e.bootstrap)
                statics = [self.reader.poolEntry(tpl[2][i]) for i in range(tpl[1])]
                if not statics:
                    return None
                first = statics[0]
                recipe = str(self.reader.poolEntry(first.index)) if type(first).__name__ == "JVMStringReference" else str(first)
                consts = []
                for s in statics[1:]:
                    consts.append(str(self.reader.poolEntry(s.index)) if type(s).__name__ == "JVMStringReference" else str(s))
                return recipe, consts
            except Exception:
                return None

        def method_at(self, addr):
            """(name, descriptor, access_flags) of the method whose code starts at addr"""
            if isinstance(self.meta, dict):
                for m in self.meta.get("methods", []) or []:
                    if m.get("address") == addr:
                        return str(m["name"]), str(m["descriptor"]), int(m.get("access_flags", 0))
            try:
                for m in self.reader.classStruct.methods:
                    if METHOD_BASE + METHOD_STRIDE * m.index == addr:
                        return m.name, m.descriptor, m.access_flags
            except Exception:
                pass
            return None

        def exception_table(self, func):
            """[[start, end, handler, 'java/io/IOException' or ''], ...] relative to the function start"""
            try:
                table = func.query_metadata("jvm.exception_table")
                if table is not None:
                    return [[int(a), int(b), int(c), str(d)] for a, b, c, d in table]
            except Exception:
                pass
            try:
                for m in self.reader.classStruct.methods:
                    if METHOD_BASE + METHOD_STRIDE * m.index == func.start and m.code_attribute is not None:
                        out = []
                        for e in m.code_attribute.attribute.exception_table:
                            ctype = str(self.reader.poolEntry(e[3])) if e[3] else ""
                            out.append([e[0], e[1], e[2], ctype])
                        return out
            except Exception:
                pass
            return []

    def recv_none_valueof(shape):
        return shape[4] is None and shape[2] == "valueOf"

    class PseudoJavaFunction(LanguageRepresentationFunction):
        comment_start_string = "// "
        annotation_start_string = "/* "
        annotation_end_string = " */"

        # --- setup -------------------------------------------------------------------------------
        def _setup(self):
            if getattr(self, "_ready", False):
                return
            self._ready = True
            func = self.function
            self.info = _ClassInfo.get(func.view)
            self.method = self.info.method_at(func.start)
            self.is_static = bool(self.method and self.method[2] & ACC_STATIC)
            self.returns_void = bool(self.method and self.method[1].endswith(")V"))
            self.return_code = self.method[1][self.method[1].index(")") + 1] if self.method else None
            # parameters by local slot (BN drops unused parameters from the function type, so positions lie)
            self.param_codes = {}
            self.this_var = None
            if self.method is not None:
                slots = arg_slots(self.method[1], self.is_static)
                for var in param_vars_by_slot(func).items():
                    slot, v = var
                    if slot == 0 and not self.is_static:
                        self.this_var = v
                    elif slot in slots:
                        self.param_codes[v] = slots[slot][1]
            self.table = self.info.exception_table(func)
            # javac's synchronized-block cleanup handlers (catch-any doing monitorexit) are not try/finally
            self.try_groups = [g for g in group_try_entries(self.table)
                               if not all(not t and self.is_monitor_cleanup(h) for h, t in g[2])]
            self.handler_types = {}
            for g in self.try_groups:
                for h, t in g[2]:
                    self.handler_types[h] = t
            for _, _, h, t in self.table:
                self.handler_types.setdefault(h, t)
            self.placed = set()
            self.placed_handlers = set()  # handler pcs whose try statement is printed (catch body goes there)
            self.consumed = set()         # expr_index of statements printed as a catch/finally body
            self.hoisted = set()
            self.active_tries = []
            self.exc_names = []           # name `exc` prints as inside a catch body
            self.hidden_gotos = {}        # label id -> gotos to it not printed
            self._follow = []             # label reached after the statement being printed (or None)
            self._sites = None
            self.monitor_handlers = {h for _, _, h, t in self.table if not t and self.is_monitor_cleanup(h)}
            # code of those handlers: astore; aload; monitorexit (self-covered range); aload; athrow
            self.monitor_windows = []
            for h in self.monitor_handlers:
                ends = [e for s, e, hh, _ in self.table if hh == h and s >= h]
                self.monitor_windows.append((h, (max(ends) if ends else h + 4) + 3))
            self._in_finally = False
            self.inline = {}        # folded single-use temporary -> its value (expr, or ('new', entry)) (jvm-46)
            self.active_syncs = []  # lock variables of the synchronized blocks being printed (jvm-47)
            self.hidden_vars = set()  # lock variables whose definition moved into a synchronized header
            self._def_counts = None
            self.deferred = set()           # expr_index of handler code met in a try body, printed by its catch
            self.printed_handlers = set()   # handler pcs whose catch/finally clause was printed
            self.var_subst = {}     # subroutine parameter -> caller expression (jsr finally bodies, jvm-51)
            self._finally_subs = None
            self._concat = {}       # expr_index of toString / indy call -> [(expr|str, is_string)]
            self._var_counts = None
            self._exc_storage = None
            try:
                self._exc_storage = func.arch.regs["exc"].index if "exc" in func.arch.regs else None
            except Exception:
                pass

        def is_monitor_cleanup(self, handler_pc):
            """the handler's first instructions (astore; aload; monitorexit; ...) release a monitor"""
            try:
                from .opcodes import decode_instruction
                data = self.function.view.read(self.function.start + handler_pc, 16)
                off = 0
                for _ in range(4):
                    name, _, length, _ = decode_instruction(data[off:], 0)
                    if name is None:
                        return False
                    if name == "monitorexit":
                        return True
                    off += length
            except Exception:
                pass
            return False

        def perform_init_token_emitter(self, emitter):
            emitter.brace_requirement = BraceRequirement.BracesAlwaysRequired
            emitter.default_braces_on_same_line = True
            emitter.has_collapsable_regions = True

        def perform_begin_lines(self, instr, tokens):
            self._setup()
            tokens.prepend_blank_collapse_indicator()
            tokens.append_open_brace()
            tokens.new_line()
            tokens.increase_indent()

        def perform_end_lines(self, instr, tokens):
            tokens.decrease_indent()
            tokens.prepend_blank_collapse_indicator()
            tokens.append_close_brace()
            tokens.new_line()

        # --- small token helpers ------------------------------------------------------------------
        def kw(self, tokens, text):
            tokens.append(_tok(TT.KeywordToken, text))

        def txt(self, tokens, text):
            tokens.append(_tok(TT.TextToken, text))

        def op(self, tokens, text):
            tokens.append(_tok(TT.OperationToken, text))

        def type_tok(self, tokens, text):
            tokens.append(_tok(TT.TypeNameToken, text))

        def note(self, tokens, text):
            tokens.append(_tok(TT.CommentToken, text))

        def args(self, tokens, settings, params):
            tokens.append_open_paren()
            for i, p in enumerate(params):
                if i:
                    self.txt(tokens, ", ")
                self.expr(p, tokens, settings)
            tokens.append_close_paren()

        def expr(self, instr, tokens, settings, precedence=None):
            self.perform_get_expr_text(instr, tokens, settings,
                                       P.TopLevelOperatorPrecedence if precedence is None else precedence)

        # --- classification -----------------------------------------------------------------------
        def is_exc_var(self, var):
            try:
                if var.source_type == VariableSourceType.RegisterVariableSourceType and self._exc_storage is not None:
                    if var.storage == self._exc_storage:
                        return True
                name = var.name
                return name == "exc" or name.startswith("exc_")
            except Exception:
                return False

        def mentions_exc(self, instr):
            for i in _walk(instr):
                if i.operation in (Op.HLIL_VAR, Op.HLIL_VAR_SSA) and (self.is_exc_var(i.var) or
                                                                       i.var in self.type_temps()):
                    return True
                if i.operation in (Op.HLIL_VAR_INIT, Op.HLIL_VAR_DECLARE):
                    if self.is_exc_var(i.dest if i.operation == Op.HLIL_VAR_INIT else i.var):
                        return True
            return False

        def is_exc_plumbing(self, s):
            """statements of the lifter's exception model that have no Java counterpart:
            `exc = __exception()`, `exc = 0`, `exc = exc_2`, declarations of exc"""
            o = s.operation
            if o == Op.HLIL_VAR_DECLARE:
                return self.is_exc_var(s.var) or s.var in self.type_temps()
            if o == Op.HLIL_VAR_INIT and s.dest in self.type_temps():
                return True
            if o == Op.HLIL_ASSIGN and s.dest.operation == Op.HLIL_VAR and s.dest.var in self.type_temps():
                return True
            if o == Op.HLIL_ASSIGN and s.dest.operation == Op.HLIL_VAR and self.is_exc_var(s.dest.var):
                return self.throw_value(s) is None
            if o == Op.HLIL_VAR_INIT and self.is_exc_var(s.dest):
                return self.throw_value(s) is None
            if o == Op.HLIL_INTRINSIC and s.intrinsic.name == "__exception":
                return True
            return False

        def is_type_test(self, e):
            return e.operation == Op.HLIL_INTRINSIC and e.intrinsic.name in ("instanceof", "__instanceof") and \
                e.params and self.is_exc_value(e.params[0]) and self.mentions_exc(e.params[0]) and \
                not self.user_instanceof(e)

        def user_instanceof(self, e):
            """an instanceof the method's code does itself (`if (e instanceof T)` in a catch body, on the caught
            exception BN propagated into exc): its address holds the instanceof opcode -- the lifter's handler
            dispatch tests sit on the throwing instruction"""
            try:
                return self.function.view.read(e.address, 1) == b"\xc1"
            except Exception:
                return False

        def type_temps(self):
            """variables BN parks a handler's type test in: `temp0 = instanceof(exc, T)`"""
            if getattr(self, "_type_temps", None) is None:
                self._type_temps = set()
                cands = {}
                try:
                    for i in _walk(self.hlil.root):
                        if i.operation == Op.HLIL_VAR_INIT:
                            cands.setdefault(i.dest, []).append(i.src)
                        elif i.operation == Op.HLIL_ASSIGN and i.dest.operation == Op.HLIL_VAR:
                            cands.setdefault(i.dest.var, []).append(i.src)
                    self._type_temps = {v for v, srcs in cands.items() if all(self.is_type_test(x) for x in srcs)}
                except Exception:
                    pass
            return self._type_temps

        def throw_value(self, s):
            """`exc = v` (athrow inside a try range) -> v; None for the lifter's own exc updates"""
            src = s.src
            if src.operation in (Op.HLIL_CONST, Op.HLIL_CONST_PTR):
                return None
            if src.operation == Op.HLIL_INTRINSIC and src.intrinsic.name.startswith("__"):
                return None
            if src.operation == Op.HLIL_VAR and self.is_exc_var(src.var):
                return None
            return src

        def is_exc_value(self, e):
            """exc, a copy of it (`T v = exc` caught-exception variable) or __exception()"""
            if e.operation in (Op.HLIL_VAR, Op.HLIL_VAR_SSA):
                return self.is_exc_var(e.var) or e.var in self.exc_copies()
            if e.operation == Op.HLIL_INTRINSIC and e.intrinsic.name == "__exception":
                return True
            return False

        def exc_copies(self):
            """variables set from exc (the caught-exception variables of handlers)"""
            if getattr(self, "_exc_copies", None) is None:
                out = set()
                try:
                    for i in _walk(self.hlil.root):
                        if i.operation == Op.HLIL_VAR_INIT and i.src.operation == Op.HLIL_VAR and \
                                self.is_exc_var(i.src.var):
                            out.add(i.dest)
                        elif i.operation == Op.HLIL_ASSIGN and i.dest.operation == Op.HLIL_VAR and \
                                i.src.operation == Op.HLIL_VAR and self.is_exc_var(i.src.var):
                            out.add(i.dest.var)
                except Exception:
                    pass
                self._exc_copies = out
            return self._exc_copies

        def exc_copy_of(self, s):
            """`T v = exc` / `v = exc` -> v, else None"""
            if s.operation == Op.HLIL_VAR_INIT and s.src.operation == Op.HLIL_VAR and self.is_exc_var(s.src.var):
                return s.dest
            if s.operation == Op.HLIL_ASSIGN and s.dest.operation == Op.HLIL_VAR and \
                    s.src.operation == Op.HLIL_VAR and self.is_exc_var(s.src.var):
                return s.dest.var
            return None

        def exc_if_parts(self, s):
            """an IF testing the exception register -> (kind, exceptional branch, other branch):
            kind 'exc' for `exc != 0` / `exc == 0` (the exceptional branch leaves to a handler or rethrows),
            'type' for a handler's type test `instanceof(exc, T) == 0` (the branch for other types);
            None for any other IF"""
            if s.operation != Op.HLIL_IF:
                return None
            test = self.exc_test(s.condition)
            if test is None:
                # a condition mixing the exception register into an ordinary test (`r != 0 && exc != 0`,
                # `exc != 0 && instanceof(exc, T) == 0`, `r == 0 || exc == 0`): on the normal path (exc == 0)
                # it has a fixed value, so the other branch is only taken on an exception
                normal = self.normal_value(s.condition) if self.mentions_exc(s.condition) else None
                if normal is None:
                    return None
                test = ('exc', not normal)
            kind, exc_true = test
            return (kind, s.true, s.false) if exc_true else (kind, s.false, s.true)

        def normal_value(self, c):
            """value of a condition on the normal path (no exception pending: exc == 0), None if it depends on
            other values"""
            if c.operation == Op.HLIL_NOT:
                v = self.normal_value(c.src)
                return None if v is None else not v
            if c.operation in (Op.HLIL_AND, Op.HLIL_OR) and c.size == 0:
                vals = [self.normal_value(c.left), self.normal_value(c.right)]
                stop = c.operation == Op.HLIL_OR  # a true operand decides ||, a false one &&
                if stop in vals:
                    return stop
                return (not stop) if all(v is not None for v in vals) else None
            test = self.exc_test(c)
            if test is not None and test[0] == 'exc':
                return not test[1]
            if test is not None and test[0] == 'type':
                return test[1]  # instanceof(null, T) is false: holds for `other types` tests
            return None

        def exc_test(self, c):
            """condition testing the exception register -> (kind, True if it holds on the exceptional
            path / for other exception types); None for any other condition (see exc_if_parts)"""
            negate = False
            while c.operation == Op.HLIL_NOT:
                negate = not negate
                c = c.src
            kind = None
            if c.operation in (Op.HLIL_CMP_NE, Op.HLIL_CMP_E):
                left, right = c.left, c.right
                if left.operation == Op.HLIL_CONST and right.operation != Op.HLIL_CONST:
                    left, right = right, left
                if right.operation not in (Op.HLIL_CONST, Op.HLIL_CONST_PTR) or right.constant != 0:
                    return None
                ne = c.operation == Op.HLIL_CMP_NE
                if self.is_exc_value(left) and self.mentions_exc(left):
                    kind, exc_true = 'exc', ne
                elif self.is_type_test(left):
                    kind, exc_true = 'type', not ne  # instanceof(...) == 0: true branch = other types
                else:
                    return None
            elif c.operation in (Op.HLIL_VAR, Op.HLIL_VAR_SSA) and self.is_exc_var(c.var):
                kind, exc_true = 'exc', True
            elif c.operation in (Op.HLIL_VAR, Op.HLIL_VAR_SSA) and c.var in self.type_temps():
                kind, exc_true = 'type', False
            elif self.is_type_test(c):
                kind, exc_true = 'type', False
            else:
                return None
            if negate:
                exc_true = not exc_true
            return kind, exc_true

        @staticmethod
        def stmts_of(instr):
            if instr is None:
                return []
            if instr.operation == Op.HLIL_BLOCK:
                return list(instr.body)
            return [instr]

        def flatten(self, stmts):
            """statements of a block with nested blocks spliced in and exception tests resolved: the normal
            path of `if (exc == 0) { ... }` / `if (exc != 0) { handler } else { ... }` continues in line,
            the exceptional branch becomes an _ExcBranch item (hidden or commented when printed)"""
            out = []
            for k, s in enumerate(stmts):
                if s.operation == Op.HLIL_BLOCK:
                    out.extend(self.flatten(s.body))
                    continue
                parts = self.exc_if_parts(s) if s.operation == Op.HLIL_IF and s.as_ast else None
                if parts is not None:
                    kind, xb, nb = parts
                    xb_empty = xb is None or xb.operation in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE)
                    if not xb_empty:
                        out.append(_ExcBranch(s, xb, kind))
                    normal = []
                    if nb is not None and nb.operation not in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE):
                        normal = self.flatten(self.stmts_of(nb))
                        out.extend(normal)
                    rest = stmts[k + 1:]
                    if xb_empty and rest and self.ends_flow(normal) and \
                            not any(i.operation == Op.HLIL_LABEL for r in rest for i in _walk(r)):
                        # `if (exc == 0) { ...; return; } rest`: rest only runs on an exception
                        out.append(_ExcBranch(s, _StmtList(rest), kind))
                        break
                    continue
                out.append(s)
            return out

        def ends_flow(self, items):
            """the last printed statement of a flattened list never falls through"""
            for s in reversed(items):
                if isinstance(s, _ExcBranch) or self.is_exc_plumbing(s) or s.operation == Op.HLIL_NOP:
                    continue
                if s.operation in (Op.HLIL_ASSIGN, Op.HLIL_VAR_INIT):
                    dest = s.dest if s.operation == Op.HLIL_VAR_INIT else \
                        (s.dest.var if s.dest.operation == Op.HLIL_VAR else None)
                    # `exc = v`: athrow inside a try range
                    return dest is not None and self.is_exc_var(dest) and self.throw_value(s) is not None
                return s.operation in (Op.HLIL_RET, Op.HLIL_BREAK, Op.HLIL_CONTINUE, Op.HLIL_GOTO, Op.HLIL_NORET,
                                       Op.HLIL_TAILCALL) or self.is_rethrow(s) or \
                    (s.operation == Op.HLIL_INTRINSIC and s.intrinsic.name in ("athrow", "__propagate"))
            return False

        def covering_handlers(self, pc):
            """handler pcs of the exception-table entries whose range holds pc"""
            return {h for s, e, h, _ in self.table if s <= pc < e}

        def in_monitor_code(self, s):
            """s lies entirely in the code of a synchronized-cleanup handler (store; monitorexit; rethrow)"""
            if not self.monitor_windows:
                return False
            start = self.function.start
            for i in _walk(s):
                pc = i.address - start
                if not any(a <= pc < b for a, b in self.monitor_windows):
                    return False
            return True

        def branch_class(self, xb, pc=None):
            """what an exceptional branch is: ('hide',) for gotos to printed handlers and pure plumbing,
            ('rethrow', cleanup statements) for `cleanup; __propagate(exc)` (a finally's rethrow path),
            ('handler', pc) for the inline body of a handler, ('goto', instr) for a goto elsewhere,
            ('code',) for anything else"""
            sites = self.sites()
            for h, (blk, idx, whole) in sites.items():
                if whole and idx == 0 and blk.expr_index == xb.expr_index:
                    return ('handler', h)
                if isinstance(xb, _StmtList) and not whole and idx < len(self.stmts_of(blk)) and \
                        self.stmts_of(blk)[idx].expr_index == xb.body[0].expr_index:
                    return ('handler', h)
            stmts = [s for s in self.flatten(self.stmts_of(xb))
                     if not isinstance(s, _ExcBranch) and not self.is_exc_plumbing(s) and
                     not self.in_monitor_code(s) and s.expr_index not in self.consumed and
                     s.operation not in (Op.HLIL_NOP, Op.HLIL_NORET, Op.HLIL_UNREACHABLE)]
            excs = [s for s in self.flatten(self.stmts_of(xb)) if isinstance(s, _ExcBranch) and
                    self.branch_class(s.branch, s.address - self.function.start)[0] not in ('hide', 'rethrow')]
            if not stmts and not excs:
                return ('hide',)
            if len(stmts) == 1 and not excs and stmts[0].operation in (Op.HLIL_GOTO, Op.HLIL_BREAK, Op.HLIL_CONTINUE):
                # a jump to the handler: hidden when that handler is printed as a catch or is javac's
                # synchronized cleanup
                if stmts[0].operation == Op.HLIL_GOTO:
                    h = self.handler_of_label(stmts[0].target)
                    if h is not None and (h in self.placed_handlers or h in self.monitor_handlers):
                        return ('hide',)
                if pc is not None:
                    covering = self.covering_handlers(pc)
                    if covering and all(h in self.placed_handlers or h in self.monitor_handlers for h in covering):
                        return ('hide',)
                return ('goto', stmts[0])
            if stmts and self.is_rethrow(stmts[-1]) and not excs:
                cleanup = stmts[:-1]
                if all(s.operation not in (Op.HLIL_GOTO, Op.HLIL_LABEL, Op.HLIL_RET) and
                       s.operation not in COMPOUND for s in cleanup):
                    return ('rethrow', cleanup)
                if self.finally_printed() and not any(i.operation == Op.HLIL_RET for c in cleanup for i in _walk(c)):
                    return ('rethrow', cleanup)  # a finally's rethrow path with branches: the finally prints it
            return ('code',)

        def is_rethrow(self, s):
            """__propagate(exc) / __propagate(caught exception variable)"""
            return s.operation == Op.HLIL_INTRINSIC and s.intrinsic.name == "__propagate" and \
                len(s.params) == 1 and self.is_exc_value(s.params[0])

        def handler_of_label(self, label):
            for h, (blk, idx, whole) in self.sites().items():
                s = blk.body[idx] if blk.operation == Op.HLIL_BLOCK else blk
                if s.operation == Op.HLIL_LABEL and s.target.label_id == label.label_id:
                    return h
            return None

        def sites(self):
            """handler pc -> (HLIL block, statement index, whole block): where the handler's code starts in
            HLIL (first statement at the handler pc, preferring labels and exception-related statements);
            whole = the block is an exceptional branch, so the handler body is the whole block"""
            if self._sites is not None:
                return self._sites
            self._sites = {}
            if not self.table:
                return self._sites
            start = self.function.start
            wanted = {h for _, _, h, _ in self.table}
            exc_branches = set()
            found = {}  # h -> [(rank, order, block, idx)]
            n = 0
            for blk, idx, s in _stmts_preorder(self.hlil.root):
                n += 1
                if s.operation == Op.HLIL_IF:
                    parts = self.exc_if_parts(s)
                    if parts is not None and parts[1] is not None and parts[1].operation == Op.HLIL_BLOCK:
                        exc_branches.add(parts[1].expr_index)
                h = s.address - start
                if h not in wanted:
                    continue
                if s.operation == Op.HLIL_LABEL:
                    rank = 0
                elif self.exc_if_parts(s) is not None or self.exc_copy_of(s) is not None:
                    rank = 1
                else:
                    rank = 2
                found.setdefault(h, []).append((rank, n, blk, idx))
            for h, cands in found.items():
                rank, _, blk, idx = min(cands, key=lambda c: (c[0], c[1]))
                self._sites[h] = (blk, idx, blk.expr_index in exc_branches)
            return self._sites

        def handler_region(self, h):
            """raw HLIL statements of handler h's body (from its site to the end of the block or the next
            label), or None"""
            site = self.sites().get(h)
            if site is None:
                return None
            blk, idx, whole = site
            body = self.stmts_of(blk)
            if whole:
                return body[idx:]
            stop = len(body)
            others = {self.stmts_of(b)[i].expr_index for hh, (b, i, w) in self.sites().items()
                      if hh != h and not w and b.expr_index == blk.expr_index}
            for k in range(idx + 1, len(body)):
                if body[k].operation == Op.HLIL_LABEL or body[k].expr_index in others:
                    stop = k  # the next label, or where another handler's code starts
                    break
            return body[idx:stop]

        def rethrow_paths(self):
            """[(lowest address, cleanup statements)] of every `cleanup; __propagate(exc)` exceptional branch"""
            if getattr(self, "_rethrows", None) is None:
                out = []
                for blk, idx, s in _stmts_preorder(self.hlil.root):
                    parts = self.exc_if_parts(s) if s.operation == Op.HLIL_IF else None
                    if parts is None or parts[1] is None:
                        continue
                    stmts = [x for x in self.flatten(self.stmts_of(parts[1])) if not isinstance(x, _ExcBranch)
                             and not self.is_exc_plumbing(x) and
                             x.operation not in (Op.HLIL_NOP, Op.HLIL_NORET, Op.HLIL_UNREACHABLE)]
                    if stmts and self.is_rethrow(stmts[-1]):
                        out.append((min(x.address for x in stmts), stmts[:-1]))
                self._rethrows = out
            return self._rethrows

        def jsr_target(self, s):
            """start of the jsr subroutine a statement calls (old javac's finally: `jsr L` lifts to a call of a
            function inside this method's code), else None"""
            c = self.stmt_call(s)
            if c.operation != Op.HLIL_CALL or c.dest.operation not in (Op.HLIL_CONST_PTR, Op.HLIL_CONST):
                return None
            t = c.dest.constant
            start = self.function.start
            return t if start < t < start + METHOD_STRIDE else None

        def finally_subs(self):
            """jsr subroutines that are finally bodies: called right before a rethrow of the caught exception
            (javac's catch-any handler `astore t; jsr L; aload t; athrow`)"""
            if self._finally_subs is None:
                out = set()
                for _, cleanup in self.rethrow_paths():
                    real = [x for x in cleanup if not self.is_exc_plumbing(x)]
                    if len(real) == 1 and self.jsr_target(real[0]) is not None:
                        out.add(self.jsr_target(real[0]))
                self._finally_subs = out
            return self._finally_subs

        def finally_printed(self):
            """a try statement with a finally clause is printed (or being printed) in this method"""
            ranges = self.placed | {(g[0], g[1]) for g in self.active_tries}
            return any((g[0], g[1]) in ranges and any(not t for _, t in g[2]) for g in self.try_groups)

        def emit_subroutine(self, call, tokens, settings):
            """the body of a jsr subroutine in place of its call (a finally body); False if its shape is not
            handled (then the call is printed)"""
            sub = self.function.view.get_function_at(self.jsr_target(call))
            hlil = sub.hlil if sub is not None else None
            if hlil is None or hlil.root is None:
                return False
            items = self.flatten(self.stmts_of(hlil.root))
            if not self.sub_shape_ok(items):
                return False
            c = self.stmt_call(call)
            subst = dict(zip(list(sub.parameter_vars), list(c.params)))
            saved = self.var_subst
            self.var_subst = dict(saved)
            self.var_subst.update(subst)
            try:
                self.emit_sub_items(items, hlil.root, tokens, settings)
            finally:
                self.var_subst = saved
            return True

        @staticmethod
        def ret_only(instr):
            body = list(instr.body) if instr.operation == Op.HLIL_BLOCK else [instr]
            return len(body) == 1 and body[0].operation == Op.HLIL_RET

        def sub_split(self, items):
            """(index, kind) of the first top-level `return` / `if (c) return;` of a subroutine body"""
            for k, s in enumerate(items):
                if isinstance(s, _ExcBranch):
                    continue
                if s.operation == Op.HLIL_RET:
                    return k, 'ret'
                if s.operation == Op.HLIL_IF and s.as_ast and self.ret_only(s.true) and \
                        (s.false is None or s.false.operation in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE)):
                    return k, 'if'
            return len(items), None

        def sub_shape_ok(self, items):
            """every `return` of the subroutine is a top-level one sub_split handles"""
            k, kind = self.sub_split(items)
            for s in items[:k]:
                if not isinstance(s, _ExcBranch) and any(i.operation == Op.HLIL_RET for i in _walk(s)):
                    return False
            return kind != 'if' or self.sub_shape_ok(items[k + 1:])

        def emit_sub_items(self, items, block, tokens, settings):
            k, kind = self.sub_split(items)
            if k:
                self.emit_list(items[:k], tokens, settings, block, own=True)
            if kind == 'if':
                # `if (c) return; rest` (the subroutine's ret) -> `if (!c) { rest }`
                self.kw(tokens, "if ")
                tokens.append_open_paren()
                self.emit_negated(items[k].condition, tokens, settings)
                tokens.append_close_paren()
                tokens.begin_scope(ScopeType.BlockScopeType)
                self.emit_sub_items(items[k + 1:], block, tokens, settings)
                tokens.end_scope(ScopeType.BlockScopeType)
                tokens.finalize_scope()
                tokens.new_line()

        def emit_negated(self, c, tokens, settings):
            flip = {Op.HLIL_CMP_E: Op.HLIL_CMP_NE, Op.HLIL_CMP_NE: Op.HLIL_CMP_E}
            if c.operation in flip:
                text, prec = BINARY[flip[c.operation]]
                self.emit_binary(text, prec, _Pair(c.left, c.right), tokens, settings, P.TopLevelOperatorPrecedence)
                return
            if c.operation == Op.HLIL_NOT:
                self.expr(c.src, tokens, settings)
                return
            self.op(tokens, "!")
            self.perform_get_expr_text(c, tokens, settings, P.UnaryOperatorPrecedence)

        def call_target(self, instr):
            """(pool index, owner, name, descriptor) of a call through a pool slot"""
            dest = instr.dest
            if dest.operation == Op.HLIL_DEREF:
                dest = dest.src
            idx = _pool_index(_const_target(dest))
            if idx is None:
                return None
            m = self.info.member(idx)
            if m is None:
                return None
            return (idx,) + tuple(m)

        def call_shape(self, instr):
            """(idx, owner, name, desc, receiver or None, args) for HLIL_CALL / invoke* intrinsics, else None"""
            if instr.operation in (Op.HLIL_CALL, Op.HLIL_TAILCALL):
                t = self.call_target(instr)
                if t is None:
                    return None
                params = list(instr.params)
            elif instr.operation == Op.HLIL_INTRINSIC and instr.intrinsic.name.startswith("invoke") and instr.params:
                idx = _pool_index(_const_target(instr.params[0]))
                m = self.info.member(idx) if idx is not None else None
                if m is None:
                    return None
                t = (idx,) + tuple(m)
                params = list(instr.params)[1:]
            else:
                return None
            idx, owner, name, desc = t
            nargs = len(descriptor_arg_codes(desc)) if desc else None
            if owner is not None and params and (nargs is None or len(params) == nargs + 1):
                return idx, owner, name, desc, params[0], params[1:]
            return idx, owner, name, desc, None, params

        @staticmethod
        def stmt_call(s):
            """the call of a call statement, also when its (void) result is assigned: `r = X.<init>(v)`"""
            if s.operation in (Op.HLIL_VAR_INIT, Op.HLIL_ASSIGN) and s.src.operation in (Op.HLIL_CALL, Op.HLIL_INTRINSIC):
                return s.src
            return s

        def new_assignment(self, s):
            """`v = new(X)` -> (var, pool index of X)"""
            if s.operation == Op.HLIL_VAR_INIT:
                var, src = s.dest, s.src
            elif s.operation == Op.HLIL_ASSIGN and s.dest.operation == Op.HLIL_VAR:
                var, src = s.dest.var, s.src
            else:
                return None
            if src.operation == Op.HLIL_INTRINSIC and src.intrinsic.name == "new" and len(src.params) == 1:
                idx = _pool_index(_const_target(src.params[0]))
                if idx is not None:
                    return var, idx
            return None

        @staticmethod
        def refs(instr, var):
            return sum(1 for i in _walk(instr) if i.operation == Op.HLIL_VAR and i.var == var)

        def var_count(self, var):
            if self._var_counts is None:
                counts = {}
                try:
                    for i in _walk(self.hlil.root):
                        if i.operation == Op.HLIL_VAR:
                            counts[i.var] = counts.get(i.var, 0) + 1
                except Exception:
                    pass
                self._var_counts = counts
            return self._var_counts.get(var, 0)

        # --- block planning (idioms + try regions) ------------------------------------------------
        def plan_block(self, body):
            plan = {"skip": set(), "new_at": {}}
            for i, s in enumerate(body):
                nv = self.new_assignment(s)
                if nv is None:
                    continue
                var, cls_idx = nv
                for j in range(i + 1, len(body)):
                    if not self.refs(body[j], var):
                        continue
                    shape = self.call_shape(self.stmt_call(body[j]))
                    if shape and shape[2] == "<init>" and shape[4] is not None and \
                            shape[4].operation == Op.HLIL_VAR and shape[4].var == var:
                        plan["skip"].add(i)
                        plan["new_at"][j] = (s, var, cls_idx, shape)
                        self.plan_concat(body, j, var, cls_idx, shape, plan)
                    break
            return plan

        def plan_concat(self, body, j, var, cls_idx, init_shape, plan):
            """sb = new StringBuilder(x); sb.append(a)...; ... sb.append(b).toString() -> x + a + b"""
            if self.info.class_ref(cls_idx) not in STRING_BUILDERS:
                return
            operands = []
            idesc = init_shape[3] or ""
            if init_shape[5] and (idesc.startswith("(Ljava/lang/String;") or
                                  idesc.startswith("(Ljava/lang/CharSequence;")):
                operands.append((init_shape[5][0], True))
            sb = var
            used = 1  # the <init> call
            appends = []
            final = None
            cur = var  # the builder as currently named: javac code may park a partial chain in a temp
            for k in range(j + 1, len(body)):
                n = self.refs(body[k], cur)
                if not n:
                    continue
                s = body[k]
                chain = self.append_chain(s, cur)
                if chain is not None and chain[1] is None:
                    appends.append(k)
                    operands.extend(chain[0])
                    used += n if cur == sb else 0
                    continue
                if s.operation == Op.HLIL_VAR_INIT and n == 1:
                    # tmp = sb.append(a)...append(b); continued through tmp (used exactly once later)
                    chain = self.append_chain(s.src, cur)
                    if chain is not None and self.var_count(s.dest) == 1 and chain[0]:
                        appends.append(k)
                        operands.extend(chain[0])
                        used += n if cur == sb else 0
                        cur = s.dest
                        continue
                # the statement holding toString()
                for e in _walk(s):
                    shape = self.call_shape(e)
                    if shape and shape[2] == "toString" and shape[1] in STRING_BUILDERS and shape[4] is not None:
                        chain = self.append_chain(shape[4], cur, top=True)
                        if chain is not None:
                            final = (k, e, chain[0])
                            break
                if final is not None:
                    used += n if cur == sb else 0
                break
            if final is None or used != self.var_count(sb):
                return
            k, e, tail = final
            operands.extend(tail)
            if not operands:
                operands = [('""', True)]
            plan["skip"].update(appends)
            plan["skip"].add(j)
            plan["new_at"].pop(j, None)
            self._concat[e.expr_index] = operands

        def append_chain(self, instr, var, top=False):
            """append(append(var, a), b) -> ([(a, is_str), (b, is_str)], None); top-level statement form when
            not top; returns None if the expression is not such a chain rooted at var"""
            ops = []
            cur = instr
            while True:
                if cur.operation == Op.HLIL_VAR and cur.var == var:
                    return list(reversed(ops)), None
                shape = self.call_shape(cur)
                if not shape or shape[2] != "append" or shape[1] not in STRING_BUILDERS or shape[4] is None \
                        or len(shape[5]) != 1:
                    return None
                desc = shape[3] or ""
                code = descriptor_arg_codes(desc)[0] if desc else "L"
                ops.append((shape[5][0], desc.startswith("(Ljava/lang/String;"), code))
                cur = shape[4]

        # --- statements ---------------------------------------------------------------------------
        def emit_block(self, instr, tokens, settings):
            is_root = instr.expr_index == self.hlil.root.expr_index
            if is_root:
                self.placed = set()
                self.placed_handlers = set()
                self.consumed = set()
                self.hoisted = set()
                self.exc_names = []
                self.hidden_gotos = {}
                self._follow = []
                self.inline = {}
                self.active_syncs = []
                self.hidden_vars = set()
                self.deferred = set()
                self.printed_handlers = set()
            self.emit_list(self.flatten(list(instr.body)), tokens, settings, instr, is_root)

        def emit_list(self, body, tokens, settings, block, is_root=False, own=False):
            """emit a flattened statement list (see flatten); own: the statements are a handler body that is
            printed here (they are marked consumed, so their original place skips them)"""
            if not body:
                return
            plan = self.plan_block(body)
            plan["root"] = is_root
            plan["own"] = {s.expr_index for s in body} if own else ()
            plan["follow"] = self._follow[-1] if self._follow else None
            self.emit_range(body, 0, len(body) - 1, plan, tokens, settings, block)

        def emit_range(self, body, lo, hi, plan, tokens, settings, block):
            """emit body[lo..hi], wrapping the try regions that fit (not already open further out)"""
            runs = []
            if self.try_groups and block.as_ast:
                start = self.function.start
                pcs = []
                for s in body[lo:hi + 1]:
                    if isinstance(s, _ExcBranch) or s.operation == Op.HLIL_LABEL or self.is_exc_plumbing(s) or \
                            s.expr_index in self.consumed:
                        pcs.append(None)
                    else:
                        pcs.append([s.address - start])
                # a try range is printed once per function, where it first fits
                runs = try_runs(pcs, self.try_groups, {(g[0], g[1]) for g in self.active_tries} | self.placed)
            run_at = {r[0] + lo: (r[0] + lo, r[1] + lo, r[2]) for r in runs}
            sync_at = {}
            if SYNC_BLOCKS and block.as_ast:
                try:
                    sync_at = self.plan_syncs(body, lo, hi, run_at, plan)
                except Exception:
                    sync_at = {}
            runs = list(run_at.values())
            for r in run_at.values():
                self.placed.add((r[2][0], r[2][1]))
                self.placed_handlers.update(h for h, _ in r[2][2])
            if FOLD_TEMPORARIES and block.as_ast:
                try:
                    self.plan_folds(body, lo, hi, run_at, plan)
                except Exception:
                    pass
            need_separator = None  # None: nothing emitted yet in this range
            idx = lo
            while idx <= hi:
                if idx in sync_at:
                    end, lock, value = sync_at[idx]
                    if need_separator is not None:
                        tokens.scope_separator()
                    try:
                        self.emit_sync(body, idx, end, lock, value, plan, tokens, settings, block)
                    except Exception as ex:
                        self.emit_error(body[idx], ex, tokens)
                    idx = end + 1
                    need_separator = True
                    continue
                if idx in run_at:
                    first, last, group = run_at[idx]
                    if need_separator is not None:
                        tokens.scope_separator()
                    try:
                        self.emit_try(body, first, last, group, plan, tokens, settings, block)
                    except Exception as ex:
                        self.emit_error(body[first], ex, tokens)
                    idx = last + 1
                    need_separator = True
                    continue
                if idx in plan.setdefault("folded", set()):
                    idx += 1
                    continue
                try:
                    need_separator = self.emit_statement(body, idx, plan, tokens, settings, block, need_separator)
                except Exception as ex:
                    self.emit_error(body[idx], ex, tokens)
                    need_separator = True
                idx += 1

        # --- folding single-use temporaries (jvm-46) -------------------------------------------------
        def def_count(self, var):
            if self._def_counts is None:
                counts = {}
                try:
                    for i in _walk(self.hlil.root):
                        if i.operation == Op.HLIL_VAR_INIT:
                            counts[i.dest] = counts.get(i.dest, 0) + 1
                        elif i.operation == Op.HLIL_VAR_DECLARE:
                            counts[i.var] = counts.get(i.var, 0) + 2  # declared apart: assigned elsewhere
                except Exception:
                    pass
                self._def_counts = counts
            return self._def_counts.get(var, 0)

        def fold_candidate(self, body, k, plan):
            """(var, value, value is free of side effects) when body[k] defines a temporary that is read once"""
            s = body[k]
            if isinstance(s, _ExcBranch):
                return None
            if k in plan["new_at"]:
                s0, var = plan["new_at"][k][0], plan["new_at"][k][1]
                if s0.operation != Op.HLIL_VAR_INIT or self.var_count(var) != 2 or self.def_count(var) != 1:
                    return None
                return var, ('new', plan["new_at"][k]), False
            if k in plan["skip"] or s.operation != Op.HLIL_VAR_INIT:
                return None
            var = s.dest
            if self.var_count(var) != 1 or self.def_count(var) != 1 or self.is_exc_var(var) or \
                    var == self.this_var or var in self.hoisted or self.is_exc_value(s.src) or \
                    self.mentions_exc(s.src):
                return None
            return var, s.src, self.pure(s.src)

        def pure(self, e):
            """no side effects (calls, stores, allocation with a constructor) when evaluated"""
            for i in self.eval_nodes(e)[0]:
                if self.side_effect(i):
                    return False
            return True

        def side_effect(self, i):
            o = i.operation
            if o in (Op.HLIL_CALL, Op.HLIL_TAILCALL, Op.HLIL_ASSIGN, Op.HLIL_VAR_INIT, Op.HLIL_ASSIGN_UNPACK):
                return i.expr_index not in self._concat or o != Op.HLIL_CALL
            if o == Op.HLIL_INTRINSIC:
                return i.intrinsic.name not in PURE_INTRINSICS
            return False

        @staticmethod
        def reads_memory(i):
            o = i.operation
            if o in (Op.HLIL_DEREF, Op.HLIL_DEREF_FIELD, Op.HLIL_ARRAY_INDEX):
                return True
            return o == Op.HLIL_INTRINSIC and i.intrinsic.name in ("getfield", "arraylength")

        def eval_nodes(self, e, stop_var=None, out=None):
            """the nodes of e in Java evaluation order (operands before the operation); folded temporaries are
            expanded to their values, a concatenation to its operands. With stop_var: stops at its read and
            returns (nodes before it, True); a read in a conditionally evaluated operand returns (.., None)"""
            if out is None:
                out = []
            o = e.operation
            if o in (Op.HLIL_VAR, Op.HLIL_VAR_SSA):
                if stop_var is not None and e.var == stop_var:
                    return out, True
                value = self.inline.get(e.var)
                if value is not None:
                    if isinstance(value, tuple):  # ('new', entry): the constructor arguments
                        for a in value[1][3][5]:
                            r = self.eval_nodes(a, stop_var, out)
                            if r[1] is not False:
                                return r
                        out.append(e)
                        return out, False
                    return self.eval_nodes(value, stop_var, out)
                out.append(e)
                return out, False
            if o in (Op.HLIL_CALL, Op.HLIL_INTRINSIC) and e.expr_index in self._concat:
                kids = [op[0] for op in self._concat[e.expr_index] if not isinstance(op[0], str)]
            elif o in (Op.HLIL_AND, Op.HLIL_OR) and e.size == 0:
                r = self.eval_nodes(e.left, stop_var, out)
                if r[1] is not False:
                    return r
                if stop_var is not None and any(i.operation == Op.HLIL_VAR and i.var == stop_var
                                                for i in _walk(e.right)):
                    return out, None  # only evaluated sometimes
                kids = [e.right]
            elif o == Op.HLIL_ASSIGN:
                d = e.dest
                kids = ([] if d.operation == Op.HLIL_VAR else list(_children(d))) + [e.src]
            elif o == Op.HLIL_VAR_INIT:
                kids = [e.src]
            elif o in (Op.HLIL_IF, Op.HLIL_SWITCH):
                kids = [e.condition]
            elif o in (Op.HLIL_WHILE, Op.HLIL_DO_WHILE, Op.HLIL_FOR, Op.HLIL_BLOCK):
                if stop_var is not None:
                    return out, None
                kids = []
            else:
                kids = list(_children(e))
            for c in kids:
                r = self.eval_nodes(c, stop_var, out)
                if r[1] is not False:
                    return r
            out.append(e)
            return out, False

        def fold_transparent(self, body, k, plan):
            """body[k] is not printed between a definition and its use, and has no effect that matters"""
            s = body[k]
            if k in plan.get("folded", ()):
                return True  # its value moved into the use
            if isinstance(s, _ExcBranch):
                if s.branch.expr_index in self.consumed:
                    return True
                cls = self.branch_class(s.branch, s.address - self.function.start)
                return cls[0] in ('hide', 'rethrow') or (cls[0] == 'handler' and cls[1] in self.placed_handlers)
            if s.expr_index in self.consumed and s.expr_index not in plan.get("own", ()):
                return True
            if self.is_exc_plumbing(s) or self.in_monitor_code(s) or s.operation == Op.HLIL_NOP:
                return True
            if k in plan["skip"]:
                if self.new_assignment(s) is not None:
                    return True
                # a StringBuilder append of a concatenation printed later: only pure operands
                return all(not self.side_effect(i) or (self.call_shape(i) or (0, 0, ""))[2] in ("append", "<init>")
                           for i in _walk(s) if i.operation != Op.HLIL_VAR_INIT)
            return False

        def plan_folds(self, body, lo, hi, run_at, plan):
            folded = plan.setdefault("folded", set())
            inside_runs = set()
            for first, last, _ in run_at.values():
                inside_runs.update(range(first, last + 1))
            site_stmts = {self.stmts_of(blk)[i].expr_index for blk, i, whole in self.sites().values()
                          if not whole and i < len(self.stmts_of(blk))}
            for k in range(hi, lo - 1, -1):
                if k in inside_runs or k in folded:
                    continue
                try:
                    self.plan_fold(body, k, hi, run_at, inside_runs, site_stmts, plan, folded)
                except Exception:
                    pass  # this statement stays as it is

        def plan_fold(self, body, k, hi, run_at, inside_runs, site_stmts, plan, folded):
            """fold body[k] into the next printed statement when that is safe (see plan_folds)"""
            c = self.fold_candidate(body, k, plan)
            if c is None or body[k].expr_index in site_stmts:
                return
            var, value, value_pure = c
            j = k + 1
            while j <= hi and j not in run_at and j not in inside_runs and self.fold_transparent(body, j, plan):
                j += 1
            if j > hi or j in run_at or j in inside_runs:
                return
            u = body[j]
            if isinstance(u, _ExcBranch) or u.operation in (Op.HLIL_LABEL, Op.HLIL_GOTO, Op.HLIL_WHILE,
                                                           Op.HLIL_DO_WHILE, Op.HLIL_FOR) or \
                    u.expr_index in site_stmts or self.is_exc_plumbing(u) or \
                    (j in plan["skip"]) or self.jsr_target(u) is not None or \
                    (u.expr_index in self.consumed and u.expr_index not in plan.get("own", ())):
                return
            uses = sum(1 for i in self.eval_nodes(u)[0] if i.operation in (Op.HLIL_VAR, Op.HLIL_VAR_SSA) and i.var == var)
            if uses != 1:  # (a concatenation's operands count: their HLIL sits in skipped append statements)
                return
            before, found = self.eval_nodes(u, var)
            if found is not True:
                return
            if any(self.side_effect(i) for i in before):
                return
            if not value_pure and any(self.reads_memory(i) for i in before):
                return
            self.inline[var] = value
            folded.add(k)

        def emit_inlined(self, value, tokens, settings, precedence):
            if isinstance(value, tuple):
                s, var, cls_idx, shape = value[1]
                self.kw(tokens, "new ")
                self.type_tok(tokens, java_class_name(self.info.class_ref(cls_idx) or "?"))
                self.typed_args(tokens, settings, shape[5], shape[3])
                return
            self.perform_get_expr_text(value, tokens, settings, precedence)

        # --- synchronized blocks (jvm-47) ------------------------------------------------------------
        def monitor_var(self, s, name):
            """the lock variable of a `monitorenter(v)` / `monitorexit(v)` statement, else None"""
            if isinstance(s, _ExcBranch) or s.operation != Op.HLIL_INTRINSIC or s.intrinsic.name != name or \
                    len(s.params) != 1 or s.params[0].operation not in (Op.HLIL_VAR, Op.HLIL_VAR_SSA):
                return None
            return s.params[0].var

        def monitor_exits(self, roots, lock):
            """monitorexit(lock) statements below roots that are not javac's cleanup-handler code"""
            n = 0
            for r in roots:
                for i in _walk(r.instr if isinstance(r, _ExcBranch) else r):
                    if self.monitor_var(i, "monitorexit") == lock and not self.in_monitor_code(i):
                        n += 1
            return n

        def never_falls(self, s):
            """control never continues after statement s (every path returns, throws, breaks, ...)"""
            if isinstance(s, _ExcBranch):
                return False
            if s.operation == Op.HLIL_IF:
                if s.false is None or s.false.operation in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE):
                    return False
                return all(self.ends_flow(self.flatten(self.stmts_of(b))) or
                           (self.flatten(self.stmts_of(b)) and self.never_falls(self.flatten(self.stmts_of(b))[-1]))
                           for b in (s.true, s.false))
            return self.ends_flow([s])

        def plan_syncs(self, body, lo, hi, run_at, plan):
            """{index of monitorenter: (last index of the block, lock var, value printed for the lock)} for the
            synchronized blocks of body[lo..hi]; try runs inside a block move into it, a run crossing one
            cancels it"""
            out = {}
            k = lo
            while k <= hi:
                lock = self.monitor_var(body[k], "monitorenter")
                if lock is None or self.in_monitor_code(body[k]):
                    k += 1
                    continue
                end = None
                for b in range(hi, k, -1):
                    if self.monitor_var(body[b], "monitorexit") == lock:
                        end = b
                        break
                last = end - 1 if end is not None else hi
                if end is None:
                    real = [x for x in range(k + 1, hi + 1) if not self.fold_transparent(body, x, plan)]
                    if not real or not self.never_falls(body[real[-1]]):
                        k += 1
                        continue
                inner = body[k + 1:(end if end is not None else hi) + 1]
                total = self.monitor_exits([self.hlil.root], lock)
                if not self.exits_leave(body, k, end if end is not None else hi + 1, lock) or \
                        self.monitor_exits(inner, lock) != total or \
                        any(self.monitor_var(i, "monitorenter") == lock for x in inner
                            for i in _walk(x.instr if isinstance(x, _ExcBranch) else x)):
                    k += 1
                    continue
                stop = end if end is not None else hi
                crossing = [f for f, (a, b, _) in run_at.items() if a <= stop and b >= k and not (k < a and b <= last)
                            and not (a <= k and stop <= b)]
                containing = [f for f, (a, b, _) in run_at.items() if a <= k and stop <= b]
                if crossing or containing:
                    k += 1
                    continue
                for f in [f for f, (a, b, _) in run_at.items() if k < a and b <= last]:
                    del run_at[f]  # placed inside the block
                out[k] = (stop, lock, self.lock_value(body, k, lock, plan))
                k = stop + 1
            return out

        def hidden_item(self, s):
            """a flattened item that prints nothing"""
            if isinstance(s, _ExcBranch):
                if s.branch.expr_index in self.consumed:
                    return True
                cls = self.branch_class(s.branch, s.address - self.function.start)
                return cls[0] in ('hide', 'rethrow') or (cls[0] == 'handler' and cls[1] in self.placed_handlers)
            return self.is_exc_plumbing(s) or s.operation == Op.HLIL_NOP or \
                (s.expr_index in self.consumed) or self.in_monitor_code(s)

        def exit_leaves(self, after):
            """the statements after a monitorexit leave the block at once (return / throw)"""
            for y in after:
                if self.hidden_item(y):
                    continue
                return not isinstance(y, _ExcBranch) and y.operation not in (Op.HLIL_BREAK, Op.HLIL_CONTINUE,
                                                                             Op.HLIL_GOTO) and self.ends_flow([y])
            return False

        def exits_leave(self, body, k, end, lock):
            """every monitorexit(lock) inside body[k+1..end) is directly followed by a return or throw (the block's
            other exits); a release in the middle of the code would put the rest outside the lock"""
            for m in range(k + 1, end):
                x = body[m]
                if isinstance(x, _ExcBranch):
                    continue
                if self.monitor_var(x, "monitorexit") == lock:
                    if not self.in_monitor_code(x) and not self.exit_leaves(body[m + 1:end]):
                        return False
                    continue
                for blk, i, st in _stmts_preorder(x):
                    if blk.operation != Op.HLIL_BLOCK or self.monitor_var(st, "monitorexit") != lock or \
                            self.in_monitor_code(st):
                        continue
                    if not self.exit_leaves(self.flatten(list(blk.body)[i + 1:])):
                        return False
            return True

        def lock_only_vars(self):
            """variables read only as the operand of monitorenter / monitorexit, set by one assignment"""
            if getattr(self, "_lock_only", None) is None:
                self._lock_only = set()
                try:
                    reads, mon, assigns = {}, {}, {}
                    for i in _walk(self.hlil.root):
                        if i.operation == Op.HLIL_VAR:
                            reads[i.var] = reads.get(i.var, 0) + 1
                        if self.monitor_var(i, "monitorenter") is not None or self.monitor_var(i, "monitorexit") is not None:
                            v = i.params[0].var
                            mon[v] = mon.get(v, 0) + 1
                        if i.operation == Op.HLIL_ASSIGN and i.dest.operation == Op.HLIL_VAR:
                            assigns[i.dest.var] = assigns.get(i.dest.var, 0) + 1
                    self._lock_only = {v for v, n in mon.items() if assigns.get(v) == 1 and reads.get(v, 0) == n + 1
                                       and self.def_count(v) == 2}  # declared apart (VAR_DECLARE), assigned once
                except Exception:
                    pass
            return self._lock_only

        def lock_value(self, body, k, lock, plan):
            """the expression the lock variable is set from right before the monitorenter, when the variable
            is used for nothing else (its definition is then not printed)"""
            try:
                defs = list(self.hlil.get_var_definitions(lock))
            except Exception:
                return None
            if len(defs) != 1:
                return None
            reads = sum(1 for i in _walk(self.hlil.root) if i.operation == Op.HLIL_VAR and i.var == lock)
            monitor = sum(1 for i in _walk(self.hlil.root)
                          if self.monitor_var(i, "monitorenter") == lock or self.monitor_var(i, "monitorexit") == lock)
            d = defs[0]
            is_assign = d.operation == Op.HLIL_ASSIGN
            if reads != monitor + (1 if is_assign else 0):
                return None
            j = k - 1
            while j >= 0 and body[j].expr_index != d.expr_index and self.fold_transparent(body, j, plan):
                j -= 1
            if j < 0 or body[j].expr_index != d.expr_index:
                return None
            plan.setdefault("folded", set()).add(j)
            if is_assign:
                self.hidden_vars.add(lock)
            return d.src

        def emit_sync(self, body, k, end, lock, value, plan, tokens, settings, block):
            # variables declared in the block but used after it are declared before it (as for try)
            for j in range(k + 1, end + 1):
                s = body[j]
                if isinstance(s, _ExcBranch) or s.operation != Op.HLIL_VAR_INIT or s.dest in self.hoisted or \
                        s.dest in self.inline or self.is_exc_var(s.dest) or j in plan["skip"]:
                    continue
                inside = sum(self.refs(body[x], s.dest) for x in range(k + 1, end + 1) if not isinstance(body[x], _ExcBranch))
                if self.var_count(s.dest) > inside:
                    self.hoisted.add(s.dest)
                    self.emit_var_decl(s.dest, s, tokens, s.src)
                    tokens.append_semicolon()
                    tokens.new_line()
            self.kw(tokens, "synchronized ")
            tokens.append_open_paren()
            if value is not None:
                self.expr(value, tokens, settings)
            else:
                self.emit_var(lock, body[k], tokens)
            tokens.append_close_paren()
            tokens.begin_scope(ScopeType.BlockScopeType)
            self.active_syncs.append(lock)
            last = end - 1 if self.monitor_var(body[end], "monitorexit") == lock else end
            try:
                self.emit_range(body, k + 1, last, plan, tokens, settings, block)
            finally:
                self.active_syncs.pop()
                tokens.end_scope(ScopeType.BlockScopeType)
            tokens.finalize_scope()
            tokens.new_line()

        def emit_error(self, s, ex, tokens):
            """never lose a statement: if rendering fails, print its HLIL as a comment"""
            try:
                import binaryninja
                binaryninja.log_warn("Pseudo Java: %s at %#x: %r" % (type(ex).__name__, s.address, ex))
            except Exception:
                pass
            tokens.new_line()
            self.note(tokens, "// [Pseudo Java could not render this statement: %s]" % type(ex).__name__)
            tokens.new_line()
            instr = s.instr if isinstance(s, _ExcBranch) else s
            try:
                text = [str(l) for l in instr.lines] if hasattr(instr, "lines") else str(instr).split("\n")
            except Exception:
                text = [str(instr)]
            for line in text:
                self.note(tokens, "// " + line)
                tokens.new_line()

        def emit_statement(self, body, idx, plan, tokens, settings, block, need_separator):
            s = body[idx]
            if idx in plan["skip"] or (s.expr_index in self.consumed and s.expr_index not in plan.get("own", ())):
                return need_separator
            if s.expr_index in self.deferred and s.expr_index not in plan.get("own", ()):
                return need_separator  # handler code: printed in its catch clause
            for h, (blk, i, whole) in self.sites().items():
                if not whole and i < len(self.stmts_of(blk)) and self.stmts_of(blk)[i].expr_index == s.expr_index \
                        and h in self.placed_handlers and h not in self.printed_handlers:
                    # the code of a handler whose catch clause is printed after this try body
                    self.deferred.update(x.expr_index for x in self.handler_region(h) or [])
                    return need_separator
            if isinstance(s, _ExcBranch):
                return self.emit_exc_branch(s, tokens, settings, need_separator)
            if (s.operation == Op.HLIL_VAR_INIT and s.dest in self.inline) or \
                    (idx in plan["new_at"] and plan["new_at"][idx][1] in self.inline):
                return need_separator  # folded into its use (jvm-46)
            if self.is_exc_plumbing(s) or self.in_monitor_code(s):
                return need_separator
            if s.operation == Op.HLIL_GOTO:
                h = self.handler_of_label(s.target)
                if h is not None and h in self.placed_handlers:
                    return need_separator  # `exc = v; goto handler` -- the throw is printed already
            if s.operation in (Op.HLIL_ASSIGN, Op.HLIL_VAR_INIT) and \
                    self.is_exc_var(s.dest if s.operation == Op.HLIL_VAR_INIT else s.dest.var if
                                    s.dest.operation == Op.HLIL_VAR else None):
                value = self.throw_value(s)
                if value is not None:  # athrow inside a try range: exc = v; goto handler
                    if need_separator:
                        tokens.scope_separator()
                    self.kw(tokens, "throw ")
                    self.expr(value, tokens, settings)
                    tokens.append_semicolon()
                    tokens.new_line()
                    return False
            for h, (blk, i, whole) in self.sites().items():
                if not whole and i < len(self.stmts_of(blk)) and self.stmts_of(blk)[i].expr_index == s.expr_index \
                        and h not in self.placed_handlers:
                    if h in self.monitor_handlers:
                        region = self.handler_region(h)
                        items = self.flatten(region)
                        if self.is_rethrow_only(items):
                            self.consume(region, items)  # synchronized cleanup: monitorexit; rethrow
                            return need_separator
                    if need_separator:
                        tokens.scope_separator()
                    ctype = self.handler_types.get(h, "")
                    self.note(tokens, "// exception handler (%s) at pc %#x:" %
                              ("catch " + java_class_name(ctype) if ctype else "finally", h))
                    tokens.new_line()
                    need_separator = False
                    break
            if (block.as_ast and s.operation == Op.HLIL_RET and plan.get("root") and
                    (len(s.src) == 0 or (self.returns_void and not self.void_return_call(s))) and
                    self.rest_hidden(body, idx, plan)):
                return need_separator
            if self.is_implicit_super(s) or s.operation == Op.HLIL_NORET:
                return need_separator
            lock = self.monitor_var(s, "monitorexit")
            if lock is not None and lock in self.active_syncs:
                return need_separator  # leaving the synchronized block
            if s.operation == Op.HLIL_VAR_DECLARE and (s.var in self.hidden_vars or s.var in self.lock_only_vars()):
                return need_separator
            if s.operation == Op.HLIL_ASSIGN and s.dest.operation == Op.HLIL_VAR and \
                    s.dest.var in self.lock_only_vars():
                # a lock variable whose synchronized block could not be printed: declared where it is set
                if need_separator:
                    tokens.scope_separator()
                self.emit_var_decl(s.dest.var, s, tokens, s.src)
                self.op(tokens, " = ")
                self.expr(s.src, tokens, settings, P.AssignmentOperatorPrecedence)
                tokens.append_semicolon()
                tokens.new_line()
                return False
            if not self.exc_names and self.is_rethrow(s) and s.params[0].operation in (Op.HLIL_VAR, Op.HLIL_VAR_SSA) \
                    and self.is_exc_var(s.params[0].var):
                return need_separator  # an uncaught exception propagates: no Java statement
            if self.jsr_target(s) in self.finally_subs():
                if self._in_finally:
                    if need_separator:
                        tokens.scope_separator()
                    if self.emit_subroutine(s, tokens, settings):
                        return True
                elif self.finally_printed():
                    return need_separator  # the finally clause prints the subroutine (jvm-51)
            call = self.void_return_call(s) if s.operation == Op.HLIL_RET and self.returns_void else None
            if call is not None:
                # `return f()` in a void method: f() is a statement (the value is the lifter's r register)
                last = plan.get("root") and self.rest_hidden(body, idx, plan)
                if not self.is_implicit_super(call):
                    if need_separator:
                        tokens.scope_separator()
                    self.perform_get_expr_text(call, tokens, settings, P.TopLevelOperatorPrecedence, True)
                    tokens.append_semicolon()
                    tokens.new_line()
                    need_separator = False
                if not last:
                    self.kw(tokens, "return")
                    tokens.append_semicolon()
                    tokens.new_line()
                    need_separator = False
                return need_separator
            if s.operation == Op.HLIL_GOTO and self.label_after(body, idx, plan) == s.target.label_id:
                # `goto L` right before L (also at the end of a branch whose statement is followed by L)
                self.hidden_gotos[s.target.label_id] = self.hidden_gotos.get(s.target.label_id, 0) + 1
                return need_separator
            if s.operation == Op.HLIL_LABEL:
                try:
                    uses = len(self.hlil.get_label_uses(s.target.label_id))
                except Exception:
                    uses = -1
                if uses >= 0 and self.hidden_gotos.get(s.target.label_id, 0) >= uses:
                    return need_separator  # every goto to it is gone
            has_blocks = s.operation in COMPOUND
            if need_separator or (need_separator is not None and has_blocks):
                tokens.scope_separator()
            if idx in plan["new_at"]:
                self.emit_new(plan["new_at"][idx], tokens, settings)
                tokens.append_semicolon()
            else:
                loop = s.operation in (Op.HLIL_WHILE, Op.HLIL_DO_WHILE, Op.HLIL_FOR, Op.HLIL_SWITCH)
                self._follow.append(None if loop else self.label_after(body, idx, plan))
                try:
                    self.perform_get_expr_text(s, tokens, settings, P.TopLevelOperatorPrecedence, True)
                finally:
                    self._follow.pop()
                if self.needs_semicolon(s):
                    tokens.append_semicolon()
            tokens.new_line()
            return has_blocks

        def rest_hidden(self, body, idx, plan):
            """nothing after body[idx] is printed (handler code moved to a catch, plumbing, ...)"""
            for k in range(idx + 1, len(body)):
                s = body[k]
                if k in plan["skip"] or (s.expr_index in self.consumed and s.expr_index not in plan.get("own", ())):
                    continue
                if isinstance(s, _ExcBranch):
                    if s.branch.expr_index in self.consumed:
                        continue
                    cls = self.branch_class(s.branch, s.address - self.function.start)
                    if cls[0] in ('hide', 'rethrow') or (cls[0] == 'handler' and cls[1] in self.placed_handlers):
                        continue
                    return False
                if self.is_exc_plumbing(s) or self.in_monitor_code(s) or \
                        s.operation in (Op.HLIL_NOP, Op.HLIL_NORET, Op.HLIL_UNREACHABLE):
                    continue
                return False
            return True

        def label_after(self, body, idx, plan):
            """label id control reaches right after body[idx] (the next printed statement is that label, or
            the list ends and its statement is followed by one), else None"""
            for k in range(idx + 1, len(body)):
                s = body[k]
                if k in plan["skip"] or isinstance(s, _ExcBranch) or self.is_exc_plumbing(s) or \
                        s.operation in (Op.HLIL_NOP,) or \
                        (s.expr_index in self.consumed and s.expr_index not in plan.get("own", ())):
                    continue
                return s.target.label_id if s.operation == Op.HLIL_LABEL else None
            return plan.get("follow")

        def emit_exc_branch(self, item, tokens, settings, need_separator):
            """the exceptional branch of an exception test: hidden when it only reaches a printed handler or
            rethrows, else kept as a commented block (never dropped)"""
            xb = item.branch
            if xb.expr_index in self.consumed:
                return need_separator
            cls = self.branch_class(xb, item.address - self.function.start)
            if cls[0] in ('hide', 'rethrow'):
                return need_separator
            if cls[0] == 'handler' and cls[1] in self.placed_handlers:
                return need_separator
            if need_separator is not None:
                tokens.scope_separator()
            if cls[0] == 'goto':
                jump = cls[1]
                if jump.operation == Op.HLIL_GOTO:
                    self.note(tokens, "// on exception: goto ")
                    tokens.append(_tok(TT.GotoLabelToken, jump.target.name, value=jump.target.label_id))
                else:
                    self.note(tokens, "// on exception: " + ("break" if jump.operation == Op.HLIL_BREAK else "continue"))
                tokens.new_line()
                return True
            if cls[0] == 'handler':
                h = cls[1]
                ctype = self.handler_types.get(h, "")
                self.note(tokens, "/* on exception -> handler (%s) at pc %#x */" %
                          ("catch " + java_class_name(ctype) if ctype else "finally", h))
                region = self.handler_region(h)
                items = self.flatten(region)
                self.consume(region, items)
                self.consumed.add(xb.expr_index)
            else:
                self.note(tokens, "/* on exception */" if item.kind == 'exc' else "/* other exception types */")
                items = [s for s in self.flatten(self.stmts_of(xb)) if s.expr_index not in self.consumed]
            tokens.begin_scope(ScopeType.BlockScopeType)
            self.emit_list(items, tokens, settings, item.instr, own=True)
            tokens.end_scope(ScopeType.BlockScopeType)
            tokens.finalize_scope()
            tokens.new_line()
            return True

        def consume(self, region, items):
            for s in list(region) + list(items):
                self.consumed.add(s.expr_index)

        def is_rethrow_only(self, items):
            real = [s for s in items if not isinstance(s, _ExcBranch) and not self.is_exc_plumbing(s) and
                    s.operation not in (Op.HLIL_NOP, Op.HLIL_NORET, Op.HLIL_UNREACHABLE, Op.HLIL_LABEL)
                    and self.exc_copy_of(s) is None]
            return bool(real) and self.is_rethrow(real[-1]) and \
                all(s.operation not in COMPOUND and s.operation not in (Op.HLIL_GOTO, Op.HLIL_RET) for s in real)

        def needs_semicolon(self, s):
            if s.operation in COMPOUND or s.operation in (Op.HLIL_LABEL, Op.HLIL_BLOCK, Op.HLIL_NOP):
                return False
            if s.operation == Op.HLIL_INTRINSIC and s.intrinsic.name in ("monitorenter", "monitorexit"):
                return False  # printed as a comment
            if s.operation == Op.HLIL_RET and self.returns_void and len(s.src) == 1 and \
                    s.src[0].operation not in (Op.HLIL_CALL, Op.HLIL_INTRINSIC):
                return True
            return True

        @staticmethod
        def void_return_call(ret):
            if len(ret.src) == 1 and ret.src[0].operation in (Op.HLIL_CALL, Op.HLIL_INTRINSIC):
                return ret.src[0]
            return None

        def is_implicit_super(self, s):
            """a no-argument `super()` in a constructor (Java inserts it implicitly)"""
            if not self.method or self.method[0] != "<init>":
                return False
            shape = self.call_shape(self.stmt_call(s))
            return bool(shape and shape[2] == "<init>" and shape[1] != self.info.class_name and not shape[5]
                        and shape[4] is not None and shape[4].operation == Op.HLIL_VAR and shape[4].var == self.this_var)

        def emit_new(self, entry, tokens, settings):
            s, var, cls_idx, shape = entry
            cls = java_class_name(self.info.class_ref(cls_idx) or "?")
            if s.operation == Op.HLIL_VAR_INIT and var not in self.hoisted:
                self.type_tok(tokens, cls)
                self.txt(tokens, " ")
            self.emit_var(var, s, tokens)
            self.op(tokens, " = ")
            self.kw(tokens, "new ")
            self.type_tok(tokens, cls)
            self.typed_args(tokens, settings, shape[5], shape[3])

        def emit_try(self, body, first, last, group, plan, tokens, settings, block):
            start, end, handlers = group
            self.active_tries.append(group)
            # variables declared in the try block but used after it are declared before it
            for k in range(first, last + 1):
                s = body[k]
                if s.operation == Op.HLIL_VAR_INIT and k not in plan["skip"] and s.dest not in self.hoisted \
                        and not self.is_exc_var(s.dest):
                    inside = sum(self.refs(body[j], s.dest) for j in range(first, last + 1))
                    if self.var_count(s.dest) > inside:
                        self.hoisted.add(s.dest)
                        self.emit_var_decl(s.dest, s, tokens, s.src)
                        tokens.append_semicolon()
                        tokens.new_line()
            for k, entry in plan["new_at"].items():
                s0, var = entry[0], entry[1]
                if first <= k <= last and s0.operation == Op.HLIL_VAR_INIT and var not in self.hoisted:
                    inside = sum(self.refs(body[j], var) for j in range(first, last + 1))
                    if self.var_count(var) > inside:
                        self.hoisted.add(var)
                        self.emit_var_decl(var, s0, tokens, s0.src)
                        tokens.append_semicolon()
                        tokens.new_line()
            self.kw(tokens, "try")
            tokens.begin_scope(ScopeType.BlockScopeType)
            try:
                self.emit_range(body, first, last, plan, tokens, settings, block)
            finally:
                tokens.end_scope(ScopeType.BlockScopeType)
                self.active_tries.pop()
            for hpc, ctype in handlers:
                self.printed_handlers.add(hpc)
                tokens.scope_continuation(False)
                try:
                    if ctype:
                        self.emit_catch(hpc, ctype, tokens, settings)
                    else:
                        self.emit_finally(hpc, tokens, settings)
                except Exception as ex:
                    self.emit_error(body[first], ex, tokens)
                    tokens.end_scope(ScopeType.BlockScopeType)
            tokens.finalize_scope()
            tokens.new_line()

        def region_items(self, h):
            """handler h's body as a flattened statement list without the handler entry (its label and the
            `T v = exc` store): (items, caught-exception variable or None, raw block); (None, None, None) if
            the handler's code is not found or was printed already"""
            region = self.handler_region(h)
            if not region or region[0].expr_index in self.consumed:
                return None, None, None
            items = self.flatten(region)
            self.consume(region, items)
            blk, _, whole = self.sites()[h]
            if whole:
                self.consumed.add(blk.expr_index)
            var = None
            out = []
            entry = True  # still in the handler entry (labels, type tests, plumbing)
            for s in items:
                if entry and s.operation == Op.HLIL_LABEL:
                    continue
                if entry and var is None and not isinstance(s, _ExcBranch):
                    v = self.exc_copy_of(s)
                    if v is not None:
                        var = v
                        continue
                if not isinstance(s, _ExcBranch) and not self.is_exc_plumbing(s):
                    entry = False
                out.append(s)
            return out, var, blk

        def catch_header(self, word, ctype, var, tokens):
            self.kw(tokens, word + " ")
            tokens.append_open_paren()
            self.type_tok(tokens, java_class_name(ctype))
            self.txt(tokens, " ")
            name = java_var_name(var.name) if var is not None else "e"
            if var is not None:
                tokens.append(_tok(TT.LocalVariableToken, name, value=var.identifier,
                                   context=InstructionTextTokenContext.LocalVariableTokenContext))
            else:
                self.txt(tokens, name)
            tokens.append_close_paren()
            return name

        def emit_handler_body(self, items, name, blk, tokens, settings):
            self.exc_names.append(name)
            try:
                self.emit_list(items, tokens, settings, blk, own=True)
            finally:
                self.exc_names.pop()

        def emit_catch(self, hpc, ctype, tokens, settings):
            items, var, blk = self.region_items(hpc)
            name = self.catch_header("catch", ctype, var, tokens)
            tokens.begin_scope(ScopeType.BlockScopeType)
            if items is None:
                self.note(tokens, "// handler at pc %#x%s" % (hpc, self.handler_hint(hpc)))
                tokens.new_line()
            else:
                self.emit_handler_body(items, name, blk, tokens, settings)
            tokens.end_scope(ScopeType.BlockScopeType)

        def emit_finally(self, hpc, tokens, settings):
            """catch-all handler: `finally { cleanup }` when its code ends by rethrowing the caught exception,
            else `catch (Throwable t) { ... }`"""
            saved = self._in_finally
            self._in_finally = True
            try:
                self.emit_finally_body(hpc, tokens, settings)
            finally:
                self._in_finally = saved

        def emit_finally_body(self, hpc, tokens, settings):
            items, var, blk = self.region_items(hpc)
            if items is not None:
                real = [k for k, s in enumerate(items) if isinstance(s, _ExcBranch) or not (
                    self.is_exc_plumbing(s) or s.operation in (Op.HLIL_NOP, Op.HLIL_NORET, Op.HLIL_UNREACHABLE)
                    or (s.operation == Op.HLIL_GOTO and self.handler_of_label(s.target) is not None))]
                last = items[real[-1]] if real else None
                rethrow = last is not None and not isinstance(last, _ExcBranch) and (
                    self.is_rethrow(last) or
                    (last.operation in (Op.HLIL_ASSIGN, Op.HLIL_VAR_INIT) and self.throw_value(last) is not None and
                     self.is_exc_value(self.throw_value(last))))
                if rethrow:
                    self.kw(tokens, "finally")
                    tokens.begin_scope(ScopeType.BlockScopeType)
                    self.emit_handler_body(items[:real[-1]], java_var_name(var.name) if var else "t", blk,
                                           tokens, settings)
                else:
                    name = self.catch_header("catch", "java/lang/Throwable", var, tokens)
                    tokens.begin_scope(ScopeType.BlockScopeType)
                    self.emit_handler_body(items, name, blk, tokens, settings)
                tokens.end_scope(ScopeType.BlockScopeType)
                return
            self.kw(tokens, "finally")
            tokens.begin_scope(ScopeType.BlockScopeType)
            # the handler's code only exists inlined into the rethrow paths `cleanup; __propagate(exc)`
            start = self.function.start + hpc
            paths = sorted((p for p in self.rethrow_paths() if p[0] >= start), key=lambda p: p[0])
            if paths:
                if paths[0][1]:
                    self.emit_list(paths[0][1], tokens, settings, paths[0][1][0], own=True)
            else:
                self.note(tokens, "// handler at pc %#x%s" % (hpc, self.handler_hint(hpc)))
                tokens.new_line()
            tokens.end_scope(ScopeType.BlockScopeType)

        def emit_body(self, instr, tokens, settings, newline=True):
            """a block, or a single statement terminated like one"""
            if instr.operation != Op.HLIL_BLOCK and self.is_exc_plumbing(instr):
                return
            self.perform_get_expr_text(instr, tokens, settings, P.TopLevelOperatorPrecedence, True)
            if instr.operation != Op.HLIL_BLOCK:
                if self.needs_semicolon(instr):
                    tokens.append_semicolon()
                if newline:
                    tokens.new_line()

        def catch_hint(self, instr):
            """' T' for an exception check: the handlers covering its pc"""
            pc = instr.address - self.function.start
            found = []
            for start, end, handlers in self.try_groups:
                if start <= pc < end:
                    found += [java_class_name(t) if t else "finally" for _, t in handlers]
            return " " + " / ".join(found) if found else ""

        def handler_hint(self, hpc):
            sym = self.function.view.get_symbol_at(self.function.start + hpc)
            return " (%s)" % sym.name if sym is not None else ""

        # --- expressions --------------------------------------------------------------------------
        def perform_get_expr_text(self, instr, tokens, settings, precedence=P.TopLevelOperatorPrecedence,
                                  statement=False):
            self._setup()
            with tokens.expr(instr):
                if instr.as_ast:
                    tokens.prepend_instr_collapse_indicator(self.function, instr)
                self._expr_text(instr, tokens, settings, precedence, statement)

        def _scoped(self, instr, body, tokens, settings, scope=None, index=0):
            if self.function.is_instruction_collapsed(instr, index):
                tokens.append(_tok(TT.CollapsedInformationToken, " ..."))
                return
            tokens.begin_scope(ScopeType.BlockScopeType if scope is None else scope)
            self.emit_body(body, tokens, settings, newline=False)
            tokens.end_scope(ScopeType.BlockScopeType if scope is None else scope)

        def _loop_cond(self, instr, tokens, settings):
            """loop condition; an exception test (`while (exc == 0)`: the loop is left by an exception, which
            goes to a handler) prints as the value it has on the normal path"""
            test = self.exc_test(instr)
            if test is not None and test[0] == 'exc':
                tokens.append_open_paren()
                self.kw(tokens, "false" if test[1] else "true")
                tokens.append_close_paren()
                return
            self._cond(instr, tokens, settings)

        def _cond(self, instr, tokens, settings):
            tokens.append_open_paren()
            self.expr(instr, tokens, settings)
            tokens.append_close_paren()

        def _expr_text(self, instr, tokens, settings, precedence, statement):
            o = instr.operation
            if o == Op.HLIL_BLOCK:
                self.emit_block(instr, tokens, settings)
            elif o == Op.HLIL_IF:
                if instr.as_ast and self.exc_if_parts(instr) is not None:
                    # exception test as a single statement (not in a block): normal path in line, the
                    # exceptional branch hidden or commented (see emit_exc_branch)
                    self.emit_list(self.flatten([instr]), tokens, settings, instr)
                    return
                self.kw(tokens, "if ")
                self._cond(instr.condition, tokens, settings)
                if not instr.as_ast:
                    return
                self._scoped(instr, instr.true, tokens, settings)
                chain = instr.false
                n = 1
                while chain is not None and chain.operation not in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE):
                    tokens.scope_continuation(False)
                    tokens.prepend_instr_collapse_indicator(self.function, instr, n)
                    if chain.operation == Op.HLIL_IF:
                        self.kw(tokens, "else if ")
                        self._cond(chain.condition, tokens, settings)
                        self._scoped(instr, chain.true, tokens, settings, index=n)
                        chain = chain.false
                        n += 1
                    else:
                        self.kw(tokens, "else")
                        self._scoped(instr, chain, tokens, settings, index=n)
                        break
                tokens.finalize_scope()
            elif o == Op.HLIL_WHILE:
                self.kw(tokens, "while ")
                self._loop_cond(instr.condition, tokens, settings)
                if instr.as_ast:
                    self._scoped(instr, instr.body, tokens, settings)
                    tokens.finalize_scope()
            elif o == Op.HLIL_DO_WHILE:
                if instr.as_ast:
                    self.kw(tokens, "do")
                    self._scoped(instr, instr.body, tokens, settings)
                    tokens.scope_continuation(True)
                    self.kw(tokens, "while ")
                    self._loop_cond(instr.condition, tokens, settings)
                    tokens.append_semicolon()
                    tokens.finalize_scope()
                else:
                    self.kw(tokens, "do while ")
                    self._cond(instr.condition, tokens, settings)
            elif o == Op.HLIL_FOR:
                init = instr.init if instr.init.operation != Op.HLIL_NOP and not self.is_exc_plumbing(instr.init) \
                    else None
                update = instr.update if instr.update.operation != Op.HLIL_NOP and \
                    not self.is_exc_plumbing(instr.update) else None
                if init is None and update is None and self.exc_test(instr.condition) is not None:
                    # for (exc = __exception(); exc == 0; exc = __exception()): a loop left by an exception
                    self.kw(tokens, "while ")
                    self._loop_cond(instr.condition, tokens, settings)
                else:
                    self.kw(tokens, "for ")
                    tokens.append_open_paren()
                    if init is not None:
                        self.expr(init, tokens, settings)
                    self.txt(tokens, "; ")
                    if instr.condition.operation != Op.HLIL_NOP:
                        self.expr(instr.condition, tokens, settings)
                    self.txt(tokens, "; ")
                    if update is not None:
                        self.expr(update, tokens, settings)
                    tokens.append_close_paren()
                if instr.as_ast:
                    self._scoped(instr, instr.body, tokens, settings)
                    tokens.finalize_scope()
            elif o == Op.HLIL_SWITCH:
                self.kw(tokens, "switch ")
                self._cond(instr.condition, tokens, settings)
                if instr.as_ast:
                    if self.function.is_instruction_collapsed(instr):
                        tokens.append(_tok(TT.CollapsedInformationToken, " ..."))
                        return
                    tokens.begin_scope(ScopeType.SwitchScopeType)
                    for case in instr.cases:
                        self.perform_get_expr_text(case, tokens, settings, P.TopLevelOperatorPrecedence, True)
                        tokens.new_line()
                    if instr.default is not None and instr.default.operation not in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE):
                        tokens.prepend_instr_collapse_indicator(self.function, instr, 1)
                        self.kw(tokens, "default")
                        self.txt(tokens, ":")
                        if self.function.is_instruction_collapsed(instr, 1):
                            tokens.append(_tok(TT.CollapsedInformationToken, " ..."))
                        else:
                            tokens.begin_scope(ScopeType.CaseScopeType)
                            self.emit_body(instr.default, tokens, settings, newline=False)
                            tokens.end_scope(ScopeType.CaseScopeType)
                    tokens.end_scope(ScopeType.SwitchScopeType)
                    tokens.finalize_scope()
            elif o == Op.HLIL_CASE:
                for i, value in enumerate(instr.values):
                    if i:
                        self.txt(tokens, ":")
                        tokens.new_line()
                    self.kw(tokens, "case ")
                    self.expr(value, tokens, settings)
                self.txt(tokens, ":")
                if self.function.is_instruction_collapsed(instr):
                    tokens.append(_tok(TT.CollapsedInformationToken, " ..."))
                else:
                    tokens.begin_scope(ScopeType.CaseScopeType)
                    self.emit_body(instr.body, tokens, settings, newline=False)
                    tokens.end_scope(ScopeType.CaseScopeType)
            elif o == Op.HLIL_BREAK:
                self.kw(tokens, "break")
            elif o == Op.HLIL_CONTINUE:
                self.kw(tokens, "continue")
            elif o in (Op.HLIL_CALL, Op.HLIL_TAILCALL):
                if o == Op.HLIL_TAILCALL:
                    self.kw(tokens, "return ")
                self.emit_call(instr, tokens, settings, precedence)
            elif o == Op.HLIL_INTRINSIC:
                self.emit_intrinsic(instr, tokens, settings, precedence)
            elif o in (Op.HLIL_ZX, Op.HLIL_SX):
                self.perform_get_expr_text(instr.src, tokens, settings, precedence)
            elif o == Op.HLIL_LOW_PART:
                self.emit_cast({1: "byte", 2: "short", 4: "int", 8: "long"}.get(instr.size, "int"),
                               instr.src, tokens, settings, precedence)
            elif o == Op.HLIL_ARRAY_INDEX:
                self.perform_get_expr_text(instr.src, tokens, settings, P.MemberAndFunctionOperatorPrecedence)
                tokens.append_open_bracket()
                self.expr(instr.index, tokens, settings)
                tokens.append_close_bracket()
            elif o == Op.HLIL_VAR_INIT and statement and self.var_count(instr.dest) == 0 and \
                    instr.src.operation in (Op.HLIL_CALL, Op.HLIL_INTRINSIC):
                self.perform_get_expr_text(instr.src, tokens, settings, P.TopLevelOperatorPrecedence)
            elif o == Op.HLIL_VAR_INIT and instr.dest in self.hoisted:
                self.emit_var(instr.dest, instr, tokens)
                self.op(tokens, " = ")
                self.perform_get_expr_text(instr.src, tokens, settings, P.AssignmentOperatorPrecedence)
            elif o == Op.HLIL_VAR_INIT:
                type_name = self.emit_var_decl(instr.dest, instr, tokens, instr.src)
                self.op(tokens, " = ")
                self.emit_typed(instr.src, TYPE_CODES.get(type_name, 'L'), tokens, settings,
                                P.AssignmentOperatorPrecedence)
            elif o == Op.HLIL_VAR_DECLARE:
                self.emit_var_decl(instr.var, instr, tokens)
            elif o == Op.HLIL_FLOAT_CONST:
                c = instr.constant
                text = repr(float(c))
                if text in ("nan", "inf", "-inf"):
                    text = {"nan": "Double.NaN", "inf": "Double.POSITIVE_INFINITY",
                            "-inf": "Double.NEGATIVE_INFINITY"}[text]
                    if instr.size == 4:
                        text = text.replace("Double", "Float")
                elif instr.size == 4:
                    text += "f"
                tokens.append(_tok(TT.FloatingPointToken, text))
            elif o == Op.HLIL_CONST:
                self.emit_const(instr, tokens, settings, precedence)
            elif o == Op.HLIL_CONST_PTR or o == Op.HLIL_IMPORT or o == Op.HLIL_EXTERN_PTR:
                self.emit_pointer(instr, tokens, settings, precedence)
            elif o == Op.HLIL_CONST_DATA:
                data, _ = instr.constant_data.data_and_builtin
                tokens.append(_tok(TT.StringToken, java_string_literal(bytes(data).decode("latin-1"))))
            elif o == Op.HLIL_VAR and self.inline and instr.var in self.inline:
                self.emit_inlined(self.inline[instr.var], tokens, settings, precedence)
            elif o == Op.HLIL_VAR and self.var_subst and instr.var in self.var_subst:
                self.perform_get_expr_text(self.var_subst[instr.var], tokens, settings, precedence)
            elif o == Op.HLIL_VAR:
                self.emit_var(instr.var, instr, tokens)
            elif o == Op.HLIL_ASSIGN:
                self.emit_assign(instr, tokens, settings)
            elif o == Op.HLIL_ASSIGN_UNPACK:
                for i, d in enumerate(instr.dest):
                    if i:
                        self.txt(tokens, ", ")
                    self.expr(d, tokens, settings)
                self.op(tokens, " = ")
                self.perform_get_expr_text(instr.src, tokens, settings, P.AssignmentOperatorPrecedence)
            elif o in (Op.HLIL_STRUCT_FIELD, Op.HLIL_DEREF_FIELD):
                parens = precedence > P.MemberAndFunctionOperatorPrecedence
                if parens:
                    tokens.append_open_paren()
                self.perform_get_expr_text(instr.src, tokens, settings, P.MemberAndFunctionOperatorPrecedence)
                self.append_field_text_tokens(instr.src, instr.offset, instr.member_index, instr.size, tokens)
                if parens:
                    tokens.append_close_paren()
            elif o == Op.HLIL_DEREF:
                self.emit_deref(instr, tokens, settings, precedence)
            elif o == Op.HLIL_ADDRESS_OF:
                self.op(tokens, "&")
                self.perform_get_expr_text(instr.src, tokens, settings, P.UnaryOperatorPrecedence)
            elif o in BINARY:
                text, prec = BINARY[o]
                if o == Op.HLIL_AND and instr.size == 0:
                    text, prec = "&&", P.LogicalAndOperatorPrecedence
                elif o == Op.HLIL_OR and instr.size == 0:
                    text, prec = "||", P.LogicalOrOperatorPrecedence
                self.emit_binary(text, prec, instr, tokens, settings, precedence)
            elif o == Op.HLIL_NOT:
                parens = precedence > P.UnaryOperatorPrecedence
                if parens:
                    tokens.append_open_paren()
                self.op(tokens, "!" if instr.size == 0 or isinstance(instr.expr_type, BoolType) else "~")
                self.perform_get_expr_text(instr.src, tokens, settings, P.UnaryOperatorPrecedence)
                if parens:
                    tokens.append_close_paren()
            elif o in (Op.HLIL_NEG, Op.HLIL_FNEG):
                parens = precedence > P.UnaryOperatorPrecedence
                if parens:
                    tokens.append_open_paren()
                self.op(tokens, "-")
                self.perform_get_expr_text(instr.src, tokens, settings, P.UnaryOperatorPrecedence)
                if parens:
                    tokens.append_close_paren()
            elif o in (Op.HLIL_FLOAT_CONV, Op.HLIL_INT_TO_FLOAT):
                self.emit_cast("float" if instr.size == 4 else "double", instr.src, tokens, settings, precedence)
            elif o in (Op.HLIL_FLOAT_TO_INT, Op.HLIL_FTRUNC):
                self.emit_cast("int" if instr.size == 4 else "long", instr.src, tokens, settings, precedence)
            elif o == Op.HLIL_BOOL_TO_INT:
                parens = precedence > P.TernaryOperatorPrecedence
                if parens:
                    tokens.append_open_paren()
                self.perform_get_expr_text(instr.src, tokens, settings, P.LogicalOrOperatorPrecedence)
                self.op(tokens, " ? 1 : 0")
                if parens:
                    tokens.append_close_paren()
            elif o in FUNCLIKE:
                self.emit_funclike(FUNCLIKE[o], instr, tokens, settings)
            elif o == Op.HLIL_RET:
                self.kw(tokens, "return")
                if len(instr.src) > 0 and not self.returns_void:
                    self.txt(tokens, " ")
                    for i, operand in enumerate(instr.src):
                        if i:
                            self.txt(tokens, ", ")
                        self.emit_typed(operand, self.return_code, tokens, settings)
            elif o == Op.HLIL_NORET:
                self.note(tokens, "// no return")
            elif o == Op.HLIL_UNREACHABLE:
                self.note(tokens, "// unreachable")
            elif o == Op.HLIL_UNDEF:
                self.note(tokens, "/* undefined */")
            elif o == Op.HLIL_NOP:
                pass
            elif o == Op.HLIL_JUMP:
                self.note(tokens, "/* jump -> ")
                self.expr(instr.dest, tokens, settings)
                self.note(tokens, " */")
            elif o == Op.HLIL_GOTO:
                self.kw(tokens, "goto ")
                tokens.append(_tok(TT.GotoLabelToken, instr.target.name, value=instr.target.label_id))
            elif o == Op.HLIL_LABEL:
                self.emit_label(instr, tokens)
            elif o == Op.HLIL_SPLIT:
                # long results come back as rh:r -- one value in Java
                self.expr(instr.low, tokens, settings)
            elif o in (Op.HLIL_UNIMPL, Op.HLIL_UNIMPL_MEM):
                self.note(tokens, "/* ")
                for token in instr.tokens:
                    token.type = TT.CommentToken
                    tokens.append(token)
                self.note(tokens, " */")
            else:
                self.note(tokens, "/* %s */" % instr.operation.name)

        # --- expression pieces --------------------------------------------------------------------
        def emit_label(self, instr, tokens):
            # same trick as pseudo_python: drop one indentation level for the label line
            tokens.init_line()
            new_tokens = tokens.current_tokens
            for i in range(len(new_tokens) - 1, -1, -1):
                if new_tokens[i].type == TT.IndentationToken:
                    new_tokens.pop(i)
                    break
            tokens.current_tokens = new_tokens
            tokens.append(_tok(TT.GotoLabelToken, instr.target.name, value=instr.target.label_id))
            self.txt(tokens, ":")

        def emit_var(self, var, instr, tokens):
            if self.this_var is not None and var == self.this_var:
                tokens.append(_tok(TT.KeywordToken, "this"))
                return
            if self.is_exc_var(var):
                # outside a catch body the lifter's exception register has no Java name: print it as `exc`, so a
                # leak of the plumbing is visible (and caught by the checks) instead of passing as `e`
                tokens.append(_tok(TT.LocalVariableToken, self.exc_names[-1] if self.exc_names else "exc", context=InstructionTextTokenContext.LocalVariableTokenContext,
                                   address=instr.expr_index, value=var.identifier, size=instr.size))
                return
            tokens.append(_tok(TT.LocalVariableToken, java_var_name(var.name), address=instr.expr_index,
                               size=instr.size, value=var.identifier,
                               context=InstructionTextTokenContext.LocalVariableTokenContext))

        def emit_var_decl(self, var, instr, tokens, src=None):
            # the initializer's type (descriptors, casts, `new`) beats the variable's propagated BN type: the
            # stack registers are shared by unrelated values, so BN's type for them is often a neighbour's
            type_name = self.expr_java_type(src) if src is not None else None
            if type_name is None and src is None:
                type_name = self.defs_java_type(var)
            if type_name is None:
                code = self.var_code(var)  # e.g. `i = 0` ... `i++`: an int whatever BN propagated
                type_name = PRIMITIVES.get(code) if code and code in "BCDFIJSZ" else java_type_of(var.type)
            self.type_tok(tokens, type_name)
            self.txt(tokens, " ")
            tokens.append(_tok(TT.LocalVariableToken, java_var_name(var.name), address=instr.expr_index,
                               size=instr.size, value=var.identifier,
                               context=InstructionTextTokenContext.LocalVariableTokenContext))
            return type_name

        def defs_java_type(self, var):
            """Java type of a declared-only variable when all its assignments agree (the lock of a synchronized
            block is a stack register BN types as int32_t)"""
            types = set()
            try:
                for d in self.hlil.get_var_definitions(var):
                    if d.operation not in (Op.HLIL_VAR_INIT, Op.HLIL_ASSIGN):
                        return None
                    types.add(self.expr_java_type(d.src))
            except Exception:
                return None
            return types.pop() if len(types) == 1 else None

        def expr_java_type(self, e):
            """Java type of an initializer, from descriptors / pool entries (None if unknown)"""
            o = e.operation
            if o in (Op.HLIL_CALL, Op.HLIL_INTRINSIC) and e.expr_index in self._concat:
                return "String"
            if o == Op.HLIL_CALL or (o == Op.HLIL_INTRINSIC and e.intrinsic.name.startswith("invoke")):
                shape = self.call_shape(e)
                if shape and shape[3]:
                    owner, name, desc = shape[1], shape[2], shape[3]
                    if HIDE_BOXING and owner in BOXES:
                        if recv_none_valueof(shape) or (shape[4] is not None and name == BOXES[owner][0]):
                            return None
                    ret = descriptor_types(desc)[1]
                    return None if ret == "void" else ret
                return None
            if o == Op.HLIL_INTRINSIC:
                name, p = e.intrinsic.name, list(e.params)
                if name == "getfield" and len(p) == 2:
                    m = self.info.member(_pool_index(_const_target(p[1])) or 0)
                    return field_type_name(m[2]) if m and m[2] else None
                if name in ("new", "checkcast") and p:
                    return self.class_operand(p[-1] if name == "checkcast" else p[0])
                if name == "newarray" and p:
                    return NEWARRAY_TYPES.get((_const_target(p[0]) or 0) - PSEUDOMEMORY_PRIMITIVES, "?") + "[]"
                if name in ("anewarray", "multianewarray") and p:
                    cls = self.info.class_ref(_pool_index(_const_target(p[0])) or 0)
                    if cls:
                        return java_class_name(cls) + ("[]" if name == "anewarray" else "")
                if name == "instanceof":
                    return "boolean"
                if name == "arraylength":
                    return "int"
                return None
            if o == Op.HLIL_DEREF:
                idx = _pool_index(_const_target(e.src))
                m = self.info.member(idx) if idx is not None else None
                return field_type_name(m[2]) if m and m[2] else None
            if o in (Op.HLIL_ADD, Op.HLIL_SUB, Op.HLIL_MUL, Op.HLIL_DIVS, Op.HLIL_MODS, Op.HLIL_AND, Op.HLIL_OR,
                     Op.HLIL_XOR, Op.HLIL_LSL, Op.HLIL_ASR, Op.HLIL_LSR, Op.HLIL_NEG, Op.HLIL_NOT) and e.size in (4, 8):
                return "int" if e.size == 4 else "long"
            if o in (Op.HLIL_FADD, Op.HLIL_FSUB, Op.HLIL_FMUL, Op.HLIL_FDIV, Op.HLIL_FNEG, Op.HLIL_FLOAT_CONV,
                     Op.HLIL_INT_TO_FLOAT) and e.size in (4, 8):
                return "float" if e.size == 4 else "double"
            if o in (Op.HLIL_CONST_PTR, Op.HLIL_CONST):
                idx = _pool_index(e.constant)
                if idx is not None and self.info.string(idx) is not None:
                    return "String"
            return None

        def emit_assign(self, instr, tokens, settings):
            dest, src = instr.dest, instr.src
            # compound assignment: x = x + 1 -> x += 1 / x++
            if self.emit_compound(src, lambda e: self.same_lvalue(dest, e),
                                  lambda: self.expr(dest, tokens, settings), tokens, settings):
                return
            self.perform_get_expr_text(dest, tokens, settings, P.AssignmentOperatorPrecedence)
            self.op(tokens, " = ")
            self.emit_typed(src, self.code_of(dest), tokens, settings, P.AssignmentOperatorPrecedence)

        def emit_compound(self, src, is_target, emit_target, tokens, settings):
            """x = x + 1 -> x++, x = x op y -> x op= y; returns False if src is not of that form"""
            ops = {Op.HLIL_ADD: "+", Op.HLIL_SUB: "-", Op.HLIL_MUL: "*", Op.HLIL_OR: "|", Op.HLIL_AND: "&",
                   Op.HLIL_XOR: "^", Op.HLIL_LSL: "<<", Op.HLIL_ASR: ">>", Op.HLIL_LSR: ">>>",
                   Op.HLIL_FADD: "+", Op.HLIL_FSUB: "-", Op.HLIL_FMUL: "*"}
            if src.operation not in ops or src.size == 0 or not is_target(src.left):
                return False
            emit_target()
            text = ops[src.operation]
            right = src.right
            c = None
            if right.operation == Op.HLIL_CONST and right.size in (1, 2, 4, 8):
                c = right.constant & ((1 << (8 * right.size)) - 1)
                if c >= 1 << (8 * right.size - 1):
                    c -= 1 << (8 * right.size)
            if text in "+-" and c is not None and c < 0:
                text = "-" if text == "+" else "+"
                if c == -1:
                    self.op(tokens, text * 2)
                else:
                    self.op(tokens, " %s= " % text)
                    tokens.append(_tok(TT.IntegerToken, str(-c), value=-c))
            elif text in "+-" and right.operation == Op.HLIL_CONST and right.constant == 1:
                self.op(tokens, text * 2)
            else:
                self.op(tokens, " %s= " % text)
                self.perform_get_expr_text(src.right, tokens, settings, P.AssignmentOperatorPrecedence)
            return True

        @staticmethod
        def same_lvalue(a, b):
            if a.operation == Op.HLIL_VAR and b.operation == Op.HLIL_VAR:
                return a.var == b.var
            if a.operation == Op.HLIL_STRUCT_FIELD and b.operation == Op.HLIL_STRUCT_FIELD:
                return a.offset == b.offset and a.src.operation == Op.HLIL_VAR and \
                    b.src.operation == Op.HLIL_VAR and a.src.var == b.src.var
            if a.operation == Op.HLIL_DEREF and b.operation == Op.HLIL_DEREF:
                ta, tb = _const_target(a.src), _const_target(b.src)
                return ta is not None and ta == tb
            return False

        def same_field(self, obj, field_ptr, e):
            """e is getfield(obj, field_ptr) on the same variable/this"""
            if e.operation != Op.HLIL_INTRINSIC or e.intrinsic.name != "getfield" or len(e.params) != 2:
                return False
            o2, f2 = e.params
            return _const_target(f2) == _const_target(field_ptr) and \
                obj.operation == Op.HLIL_VAR and o2.operation == Op.HLIL_VAR and obj.var == o2.var

        def emit_binary(self, text, prec, instr, tokens, settings, precedence):
            if text in ("==", "!=") and instr.right.operation == Op.HLIL_CONST and instr.right.constant in (0, 1) \
                    and not isinstance(instr, _Pair) and self.code_of(instr.left) == 'Z':
                # boolean b: b != 0 -> b, b == 0 -> !b
                self.emit_not(instr.left, (text == "==") == (instr.right.constant == 0), tokens, settings, precedence)
                return
            # a - b and a / b bind like + and *; their right operand needs parentheses for another +/-, *//
            group = {P.SubOperatorPrecedence: P.AddOperatorPrecedence,
                     P.DivideOperatorPrecedence: P.MultiplyOperatorPrecedence}.get(prec, prec)
            parens = precedence > group or (precedence >= P.BitwiseOrOperatorPrecedence and
                                            prec <= P.BitwiseAndOperatorPrecedence and precedence != prec)
            if parens:
                tokens.append_open_paren()
            left = group
            right = {P.AddOperatorPrecedence: P.SubOperatorPrecedence,
                     P.MultiplyOperatorPrecedence: P.DivideOperatorPrecedence}.get(prec, prec)
            # (a + (b + c) keeps its parentheses: it matters for string concatenation)
            lhs, rhs = instr.left, instr.right
            self.perform_get_expr_text(lhs, tokens, settings, left)
            self.op(tokens, " %s " % text)
            if text in ("==", "!=") and (self.is_null_peer(lhs, rhs) or (
                    rhs.operation in (Op.HLIL_CONST, Op.HLIL_CONST_PTR) and rhs.constant == 0 and
                    not isinstance(instr, _Pair) and (self.code_of(lhs) or "") in ("L", "["))):
                self.kw(tokens, "null")
            else:
                self.perform_get_expr_text(rhs, tokens, settings, right)
            if parens:
                tokens.append_close_paren()

        def emit_not(self, e, negate, tokens, settings, precedence):
            if not negate:
                self.perform_get_expr_text(e, tokens, settings, precedence)
                return
            self.op(tokens, "!")
            self.perform_get_expr_text(e, tokens, settings, P.UnaryOperatorPrecedence)

        def is_null_peer(self, lhs, rhs):
            if rhs.operation not in (Op.HLIL_CONST, Op.HLIL_CONST_PTR) or rhs.constant != 0:
                return False
            return isinstance(lhs.expr_type, PointerType) or isinstance(rhs.expr_type, PointerType)

        def emit_cast(self, type_name, src, tokens, settings, precedence):
            parens = precedence > P.UnaryOperatorPrecedence
            if parens:
                tokens.append_open_paren()
            tokens.append_open_paren()
            self.type_tok(tokens, type_name)
            tokens.append_close_paren()
            self.txt(tokens, " ")
            self.perform_get_expr_text(src, tokens, settings, P.UnaryOperatorPrecedence)
            if parens:
                tokens.append_close_paren()

        def emit_funclike(self, name, instr, tokens, settings):
            self.txt(tokens, name)
            ops = [x for x in (getattr(instr, "left", None), getattr(instr, "right", None)) if x is not None] \
                if hasattr(instr, "left") else [instr.src]
            self.args(tokens, settings, ops)

        def emit_const(self, instr, tokens, settings, precedence):
            c = instr.constant
            if instr.size == 0 or isinstance(instr.expr_type, BoolType):
                self.kw(tokens, "true" if c else "false")
            elif c == 0 and isinstance(instr.expr_type, PointerType):
                self.kw(tokens, "null")
            elif _pool_index(c) is not None:
                self.emit_pointer(instr, tokens, settings, precedence)
            else:
                tokens.append(_tok(TT.IntegerToken, java_int_literal(c, instr.size), value=c, size=instr.size,
                                   address=instr.address))

        def emit_pointer(self, instr, tokens, settings, precedence):
            addr = instr.constant
            idx = _pool_index(addr)
            if idx is not None:
                s = self.info.string(idx)
                if s is not None:
                    tokens.append(_tok(TT.StringToken, java_string_literal(s), value=addr, address=instr.address,
                                       context=InstructionTextTokenContext.ConstStringDataTokenContext))
                    return
                cls = self.info.class_ref(idx)
                kind = self.info.kind(idx)
                if kind == "JVMClassReference" and cls:
                    tokens.append(_tok(TT.DataSymbolToken, java_class_name(cls), value=addr))
                    self.kw(tokens, ".class")
                    return
                m = self.info.member(idx)
                if m is not None and kind == "JVMFieldReference":
                    self.emit_static_field(m, addr, tokens)
                    return
            if PSEUDOMEMORY_PRIMITIVES <= addr < PSEUDOMEMORY_PRIMITIVES + 16:
                self.type_tok(tokens, NEWARRAY_TYPES.get(addr - PSEUDOMEMORY_PRIMITIVES, "?"))
                return
            if addr == 0:
                self.kw(tokens, "null")
                return
            view = self.function.view
            if view.get_symbol_at(addr) is None and not view.get_functions_containing(addr) and \
                    view.get_data_var_at(addr) is None:
                # no symbol: an int BN guessed to be a pointer
                tokens.append(_tok(TT.IntegerToken, java_int_literal(addr, instr.size or 4), value=addr))
                return
            tokens.append_pointer_text_token(instr, addr, settings, SymbolDisplayType.DisplaySymbolOnly, precedence)

        def emit_static_field(self, member, addr, tokens):
            owner, name, _ = member
            if owner:
                tokens.append(_tok(TT.TypeNameToken, java_class_name(owner)))
                self.op(tokens, ".")
            tokens.append(_tok(TT.DataSymbolToken, name, value=addr))

        def emit_deref(self, instr, tokens, settings, precedence):
            src = instr.src
            addr = _const_target(src)
            idx = _pool_index(addr)
            if idx is not None:
                m = self.info.member(idx)
                if m is not None and self.info.kind(idx) in ("JVMFieldReference", None):
                    self.emit_static_field(m, addr, tokens)
                    return
                self.emit_pointer(src, tokens, settings, precedence)
                return
            arr = self.array_access(src, instr.size)
            if arr is not None:
                base, index = arr
                self.perform_get_expr_text(base, tokens, settings, P.MemberAndFunctionOperatorPrecedence)
                tokens.append_open_bracket()
                if isinstance(index, int):
                    tokens.append(_tok(TT.IntegerToken, str(index), value=index))
                else:
                    self.expr(index, tokens, settings)
                tokens.append_close_bracket()
                return
            parens = precedence > P.UnaryOperatorPrecedence
            if parens:
                tokens.append_open_paren()
            self.op(tokens, "*")
            self.perform_get_expr_text(src, tokens, settings, P.UnaryOperatorPrecedence)
            if parens:
                tokens.append_close_paren()

        def array_access(self, addr, elem):
            """arrayref + index*elem (the lifter's element address) -> (arrayref, index); HLIL folds constant
            indices into `arrayref + k*elem` or plain `arrayref` (index 0). Index None = constant."""
            if addr.operation != Op.HLIL_ADD:
                if addr.operation in (Op.HLIL_VAR, Op.HLIL_INTRINSIC, Op.HLIL_CALL, Op.HLIL_DEREF):
                    return addr, 0
                return None
            for base, off in ((addr.left, addr.right), (addr.right, addr.left)):
                if off.operation == Op.HLIL_CONST and elem and off.constant % elem == 0 and \
                        base.operation != Op.HLIL_CONST:
                    return base, off.constant // elem
            for base, off in ((addr.left, addr.right), (addr.right, addr.left)):
                if off.operation == Op.HLIL_MUL and off.right.operation == Op.HLIL_CONST and off.right.constant == elem:
                    return base, off.left
                if off.operation == Op.HLIL_LSL and off.right.operation == Op.HLIL_CONST and (1 << off.right.constant) == elem:
                    return base, off.left
            if elem == 1:
                # byte/boolean arrays: arrayref + index -- the arrayref is the reference-typed operand
                def is_ref(e):
                    return (self.code_of(e) or "") in ("[", "L") or isinstance(e.expr_type, PointerType)
                left, right = addr.left, addr.right
                if is_ref(right) and not is_ref(left):
                    return right, left
                if is_ref(left) and not is_ref(right):
                    return left, right
                arith = (Op.HLIL_ADD, Op.HLIL_SUB, Op.HLIL_MUL, Op.HLIL_CONST)
                if left.operation in arith and right.operation not in arith:
                    return right, left
                return left, right
            return None

        def emit_receiver(self, recv, tokens, settings):
            self.perform_get_expr_text(recv, tokens, settings, P.MemberAndFunctionOperatorPrecedence)

        def emit_call(self, instr, tokens, settings, precedence):
            if instr.expr_index in self._concat:
                self.emit_concat(self._concat[instr.expr_index], tokens, settings, precedence)
                return
            shape = self.call_shape(instr)
            if shape is None:
                self.perform_get_expr_text(instr.dest, tokens, settings, P.MemberAndFunctionOperatorPrecedence)
                self.args(tokens, settings, instr.params)
                return
            self.emit_java_call(instr, shape, tokens, settings, precedence)

        def emit_java_call(self, instr, shape, tokens, settings, precedence):
            idx, owner, name, desc, recv, args = shape
            slot = PSEUDOMEMORY_TABLE + idx * POOL_STRIDE
            if owner is None:  # invokedynamic
                recipe = self.info.indy_recipe(idx) if name == "makeConcatWithConstants" else None
                if recipe is not None:
                    parts = concat_recipe_parts(recipe[0], list(args), recipe[1])
                    ops = [(p[1], False) if p[0] == 'arg' else ('"' + p[1] + '"', True) for p in parts if p[1] is not None]
                    self.emit_concat(ops, tokens, settings, precedence)
                    return
                tokens.append(_tok(TT.CodeSymbolToken, name, value=slot))
                self.annotation(tokens, " /* invokedynamic */")
                self.args(tokens, settings, args)
                return
            if HIDE_BOXING and owner in BOXES and desc:
                if recv is None and name == "valueOf" and len(args) == 1 and desc[1] == BOXES[owner][1]:
                    self.perform_get_expr_text(args[0], tokens, settings, precedence)
                    return
                if recv is not None and name == BOXES[owner][0] and not args:
                    self.perform_get_expr_text(recv, tokens, settings, precedence)
                    return
            if name == "<init>" and recv is not None:
                if recv.operation == Op.HLIL_VAR and recv.var == self.this_var and self.method and self.method[0] == "<init>":
                    self.kw(tokens, "this" if owner == self.info.class_name else "super")
                    self.typed_args(tokens, settings, args, desc)
                    return
                self.emit_receiver(recv, tokens, settings)
                self.op(tokens, ".")
                tokens.append(_tok(TT.CodeSymbolToken, "<init>", value=slot))
                self.typed_args(tokens, settings, args, desc)
                return
            if recv is not None:
                self.emit_receiver(recv, tokens, settings)
                self.op(tokens, ".")
            elif QUALIFY_OWN_STATICS or owner != self.info.class_name:
                tokens.append(_tok(TT.TypeNameToken, java_class_name(owner)))
                self.op(tokens, ".")
            tokens.append(_tok(TT.CodeSymbolToken, name, value=slot))
            self.typed_args(tokens, settings, args, desc)

        def typed_args(self, tokens, settings, args, desc):
            codes = descriptor_arg_codes(desc) if desc else []
            tokens.append_open_paren()
            for i, a in enumerate(args):
                if i:
                    self.txt(tokens, ", ")
                self.emit_typed(a, codes[i] if i < len(codes) else None, tokens, settings)
            tokens.append_close_paren()

        def emit_typed(self, e, code, tokens, settings, precedence=None):
            """a value in a context of known descriptor type: 1 -> true for Z, 'x' for C, 0 -> null for L/["""
            if e.operation in (Op.HLIL_CONST, Op.HLIL_CONST_PTR) and code:
                c = e.constant
                if code == 'Z' and c in (0, 1):
                    self.kw(tokens, "true" if c else "false")
                    return
                if code == 'C' and e.operation == Op.HLIL_CONST:
                    self.emit_char(c, tokens)
                    return
                if code in 'L[' and c == 0:
                    self.kw(tokens, "null")
                    return
                if code in 'BSIJ':
                    tokens.append(_tok(TT.IntegerToken, java_int_literal(c, 8 if code == 'J' else 4), value=c))
                    return
            self.expr(e, tokens, settings, precedence)

        def var_code(self, var):
            """descriptor code of a local that is only ever set from expressions of one known code"""
            cache = self.__dict__.setdefault("_var_code_cache", {})
            if var in cache:
                return cache[var]
            cache[var] = None  # recursion guard
            codes = set()
            try:
                for d in self.hlil.get_var_definitions(var):
                    if d.operation == Op.HLIL_VAR_INIT or d.operation == Op.HLIL_ASSIGN:
                        if d.src.operation not in (Op.HLIL_CONST, Op.HLIL_CONST_PTR):  # 0/1 fit any code
                            codes.add(self.code_of(d.src))
                    else:
                        codes.add(None)
            except Exception:
                codes.add(None)
            code = codes.pop() if len(codes) == 1 else None
            cache[var] = code
            return code

        def code_of(self, e):
            """descriptor code ('Z', 'I', 'L', ...) of an lvalue/expression where known"""
            o = e.operation
            if o == Op.HLIL_VAR:
                if e.var in self.param_codes:
                    return self.param_codes[e.var]
                code = self.var_code(e.var)  # what it is set from beats BN's (shared-register) type
                if code is not None:
                    return code
                declared = java_type_of(e.var.type)
                return {"boolean": 'Z', "char": 'C'}.get(declared)
            if o == Op.HLIL_DEREF:
                idx = _pool_index(_const_target(e.src))
                m = self.info.member(idx) if idx is not None else None
                return m[2][0] if m and m[2] else None
            if o == Op.HLIL_CALL or (o == Op.HLIL_INTRINSIC and e.intrinsic.name.startswith("invoke")):
                shape = self.call_shape(e)
                if shape and shape[3]:
                    return shape[3][shape[3].index(")") + 1]
                return None
            if o == Op.HLIL_INTRINSIC:
                if e.intrinsic.name == "getfield" and len(e.params) == 2:
                    m = self.info.member(_pool_index(_const_target(e.params[1])) or 0)
                    return m[2][0] if m and m[2] else None
                if e.intrinsic.name in ("instanceof", "__instanceof"):
                    return 'Z'
            if o in (Op.HLIL_ADD, Op.HLIL_SUB, Op.HLIL_MUL, Op.HLIL_DIVS, Op.HLIL_MODS, Op.HLIL_LSL, Op.HLIL_ASR,
                     Op.HLIL_LSR, Op.HLIL_NEG) and e.size in (4, 8):
                return 'I' if e.size == 4 else 'J'
            if isinstance(e.expr_type, BoolType):
                return 'Z'
            return None

        def emit_char(self, c, tokens):
            try:
                tokens.append(_tok(TT.CharacterConstantToken, java_string_literal(chr(c & 0xffff), "'"), value=c))
            except Exception:
                tokens.append(_tok(TT.IntegerToken, str(c), value=c))

        def annotation(self, tokens, text):
            tokens.append(_tok(TT.AnnotationToken, text))

        def emit_concat(self, operands, tokens, settings, precedence):
            parens = precedence > P.AddOperatorPrecedence
            if parens:
                tokens.append_open_paren()
            ops = list(operands)
            # "" + a + b when neither of the first two operands is a String (else + would add numbers)
            if not any(op[1] or self.is_string_expr(op[0]) for op in ops[:2]):
                ops.insert(0, ('""', True))
            for i, item in enumerate(ops):
                e = item[0]
                if i:
                    self.op(tokens, " + ")
                if isinstance(e, str):
                    tokens.append(_tok(TT.StringToken, e if e.startswith('"') else java_string_literal(e)))
                elif len(item) > 2 and item[2] == 'C' and e.operation == Op.HLIL_CONST:
                    self.emit_char(e.constant, tokens)
                else:
                    self.perform_get_expr_text(e, tokens, settings,
                                               P.AddOperatorPrecedence if i == 0 else P.SubOperatorPrecedence)
            if parens:
                tokens.append_close_paren()

        def is_string_expr(self, e):
            if isinstance(e, str):
                return True
            idx = _pool_index(_const_target(e)) if e.operation in (Op.HLIL_CONST_PTR, Op.HLIL_CONST) else None
            return idx is not None and self.info.string(idx) is not None

        def class_operand(self, e):
            idx = _pool_index(_const_target(e))
            cls = self.info.class_ref(idx) if idx is not None else None
            return java_class_name(cls) if cls else None

        def emit_intrinsic(self, instr, tokens, settings, precedence):
            name = instr.intrinsic.name
            p = list(instr.params)
            if name.startswith("invoke"):
                shape = self.call_shape(instr)
                if shape is not None:
                    self.emit_java_call(instr, shape, tokens, settings, precedence)
                    return
            if name == "getfield" and len(p) == 2:
                m = self.info.member(_pool_index(_const_target(p[1])) or 0)
                self.emit_receiver(p[0], tokens, settings)
                self.op(tokens, ".")
                tokens.append(_tok(TT.FieldNameToken, m[1] if m else "?", value=_const_target(p[1]) or 0))
                return
            if name == "putfield" and len(p) == 3:
                m = self.info.member(_pool_index(_const_target(p[1])) or 0)

                def target():
                    self.emit_receiver(p[0], tokens, settings)
                    self.op(tokens, ".")
                    tokens.append(_tok(TT.FieldNameToken, m[1] if m else "?", value=_const_target(p[1]) or 0))
                if self.emit_compound(p[2], lambda e: self.same_field(p[0], p[1], e), target, tokens, settings):
                    return
                target()
                self.op(tokens, " = ")
                self.emit_typed(p[2], m[2][0] if m and m[2] else None, tokens, settings, P.AssignmentOperatorPrecedence)
                return
            if name == "new" and len(p) == 1:
                self.kw(tokens, "new ")
                self.type_tok(tokens, self.class_operand(p[0]) or "?")
                self.txt(tokens, "()")
                return
            if name in ("newarray", "anewarray", "multianewarray") and p:
                if name == "newarray":
                    base = NEWARRAY_TYPES.get((_const_target(p[0]) or 0) - PSEUDOMEMORY_PRIMITIVES, "?")
                    dims_extra = 0
                else:
                    cls = self.info.class_ref(_pool_index(_const_target(p[0])) or 0) or "?"
                    if name == "anewarray":
                        cls = "[" + (cls if cls.startswith("[") else "L%s;" % cls)
                    total = len(cls) - len(cls.lstrip("["))
                    base = field_type_name(cls.lstrip("[")) if cls.lstrip("[")[:1] in PRIMITIVES or \
                        cls.lstrip("[").startswith("L") else java_class_name(cls.lstrip("["))
                    dims_extra = total - len(p[1:])
                self.kw(tokens, "new ")
                self.type_tok(tokens, base)
                for d in p[1:]:
                    tokens.append_open_bracket()
                    self.expr(d, tokens, settings)
                    tokens.append_close_bracket()
                self.txt(tokens, "[]" * max(dims_extra, 0))
                return
            if name == "arraylength" and len(p) == 1:
                self.emit_receiver(p[0], tokens, settings)
                self.op(tokens, ".")
                self.kw(tokens, "length")
                return
            if name in ("athrow", "__propagate") and len(p) == 1:
                self.kw(tokens, "throw ")
                self.expr(p[0], tokens, settings)
                return
            if name == "checkcast" and len(p) == 2:
                self.emit_cast(self.class_operand(p[1]) or "?", p[0], tokens, settings, precedence)
                return
            if name in ("instanceof", "__instanceof") and len(p) == 2:
                parens = precedence > P.CompareOperatorPrecedence
                if parens:
                    tokens.append_open_paren()
                self.perform_get_expr_text(p[0], tokens, settings, P.CompareOperatorPrecedence)
                self.kw(tokens, " instanceof ")
                self.type_tok(tokens, self.class_operand(p[1]) or "?")
                if parens:
                    tokens.append_close_paren()
                return
            if name in ("monitorenter", "monitorexit") and len(p) == 1:
                tokens.append(_tok(TT.CommentToken, "// synchronized (" if name == "monitorenter" else "// } synchronized ("))
                self.expr(p[0], tokens, settings)
                tokens.append(_tok(TT.CommentToken, ") {" if name == "monitorenter" else ")"))
                return
            if name == "fmod" and len(p) == 2:
                self.emit_binary("%", P.DivideOperatorPrecedence, _Pair(p[0], p[1]), tokens, settings, precedence)
                return
            tokens.append(_tok(TT.KeywordToken, name))
            self.args(tokens, settings, p)

        # --- struct fields (from pseudo_python, Java member syntax) -------------------------------
        def append_field_text_tokens(self, var, offset, member_index, size, tokens):
            var_type = var.expr_type
            if isinstance(var_type, PointerType):
                var_type = var_type.target
            if isinstance(var_type, NamedTypeReferenceType):
                target_type = var_type.target(var.function.view)
                if target_type is not None:
                    var_type = target_type
            if isinstance(var_type, StructureType):
                class Resolver:
                    def __init__(self, view, offset):
                        self.has_field = False
                        self.correct_size = False
                        self.offset = offset
                        self.view = view

                    def resolve_func(self, base_name, resolved_struct, resolved_member_index, struct_offset,
                                     adjusted_offset, member):
                        tokens.append(_tok(TT.OperationToken, "."))
                        name_list = HighLevelILTokenEmitter.names_for_outer_structure_members(
                            self.view, var_type, var) + [member.name]
                        tokens.append(_tok(TT.FieldNameToken, member.name, value=struct_offset + member.offset,
                                           typeNames=name_list))
                        self.offset = adjusted_offset - member.offset
                        self.has_field = True
                        self.correct_size = member.type is not None and size == member.type.width

                resolver = Resolver(self.function.view, offset)
                result = var_type.resolve_member_or_base_member(resolver.view, offset, 0, resolver.resolve_func)
                if result and resolver.has_field and resolver.correct_size:
                    return
                offset = resolver.offset
                tokens.append(_tok(TT.StructOffsetToken, ".__offset(%#x)" % offset, value=offset, size=size))
            # not a structure: a partial access of a (stack-slot) variable -- Java has no such thing

    class _Pair:
        """stand-in with .left/.right for rendering fmod(a, b) as a % b"""
        def __init__(self, left, right):
            self.left, self.right = left, right

    BINARY = {
        Op.HLIL_ADD: ("+", P.AddOperatorPrecedence), Op.HLIL_FADD: ("+", P.AddOperatorPrecedence),
        Op.HLIL_SUB: ("-", P.SubOperatorPrecedence), Op.HLIL_FSUB: ("-", P.SubOperatorPrecedence),
        Op.HLIL_MUL: ("*", P.MultiplyOperatorPrecedence), Op.HLIL_FMUL: ("*", P.MultiplyOperatorPrecedence),
        Op.HLIL_MULS_DP: ("*", P.MultiplyOperatorPrecedence), Op.HLIL_MULU_DP: ("*", P.MultiplyOperatorPrecedence),
        Op.HLIL_DIVS: ("/", P.DivideOperatorPrecedence), Op.HLIL_DIVU: ("/", P.DivideOperatorPrecedence),
        Op.HLIL_DIVS_DP: ("/", P.DivideOperatorPrecedence), Op.HLIL_DIVU_DP: ("/", P.DivideOperatorPrecedence),
        Op.HLIL_FDIV: ("/", P.DivideOperatorPrecedence),
        Op.HLIL_MODS: ("%", P.DivideOperatorPrecedence), Op.HLIL_MODU: ("%", P.DivideOperatorPrecedence),
        Op.HLIL_MODS_DP: ("%", P.DivideOperatorPrecedence), Op.HLIL_MODU_DP: ("%", P.DivideOperatorPrecedence),
        Op.HLIL_LSL: ("<<", P.ShiftOperatorPrecedence), Op.HLIL_ASR: (">>", P.ShiftOperatorPrecedence),
        Op.HLIL_LSR: (">>>", P.ShiftOperatorPrecedence),
        Op.HLIL_AND: ("&", P.BitwiseAndOperatorPrecedence), Op.HLIL_OR: ("|", P.BitwiseOrOperatorPrecedence),
        Op.HLIL_XOR: ("^", P.BitwiseXorOperatorPrecedence),
        Op.HLIL_CMP_E: ("==", P.EqualityOperatorPrecedence), Op.HLIL_FCMP_E: ("==", P.EqualityOperatorPrecedence),
        Op.HLIL_CMP_NE: ("!=", P.EqualityOperatorPrecedence), Op.HLIL_FCMP_NE: ("!=", P.EqualityOperatorPrecedence),
        Op.HLIL_CMP_SLT: ("<", P.CompareOperatorPrecedence), Op.HLIL_CMP_ULT: ("<", P.CompareOperatorPrecedence),
        Op.HLIL_FCMP_LT: ("<", P.CompareOperatorPrecedence),
        Op.HLIL_CMP_SLE: ("<=", P.CompareOperatorPrecedence), Op.HLIL_CMP_ULE: ("<=", P.CompareOperatorPrecedence),
        Op.HLIL_FCMP_LE: ("<=", P.CompareOperatorPrecedence),
        Op.HLIL_CMP_SGE: (">=", P.CompareOperatorPrecedence), Op.HLIL_CMP_UGE: (">=", P.CompareOperatorPrecedence),
        Op.HLIL_FCMP_GE: (">=", P.CompareOperatorPrecedence),
        Op.HLIL_CMP_SGT: (">", P.CompareOperatorPrecedence), Op.HLIL_CMP_UGT: (">", P.CompareOperatorPrecedence),
        Op.HLIL_FCMP_GT: (">", P.CompareOperatorPrecedence),
    }
    FUNCLIKE = {
        Op.HLIL_FSQRT: "Math.sqrt", Op.HLIL_FABS: "Math.abs", Op.HLIL_ABS: "Math.abs", Op.HLIL_FLOOR: "Math.floor",
        Op.HLIL_CEIL: "Math.ceil", Op.HLIL_ROUND_TO_INT: "Math.rint", Op.HLIL_ROL: "Integer.rotateLeft",
        Op.HLIL_ROR: "Integer.rotateRight", Op.HLIL_POPCNT: "Integer.bitCount",
        Op.HLIL_CLZ: "Integer.numberOfLeadingZeros", Op.HLIL_CTZ: "Integer.numberOfTrailingZeros",
        Op.HLIL_MINS: "Math.min", Op.HLIL_MAXS: "Math.max", Op.HLIL_MINU: "Math.min", Op.HLIL_MAXU: "Math.max",
        Op.HLIL_FCMP_UO: "__unordered", Op.HLIL_FCMP_O: "__ordered", Op.HLIL_ADD_OVERFLOW: "__add_overflow",
        Op.HLIL_BSWAP: "Integer.reverseBytes", Op.HLIL_RBIT: "Integer.reverse", Op.HLIL_TEST_BIT: "__test_bit",
    }
    COMPOUND = (Op.HLIL_IF, Op.HLIL_WHILE, Op.HLIL_DO_WHILE, Op.HLIL_FOR, Op.HLIL_SWITCH)

    def param_vars_by_slot(func):
        """local slot -> parameter Variable (registers l<n> / l<n>_lo of the `jvm` calling convention)"""
        out = {}
        try:
            for v in func.parameter_vars:
                slot = local_slot(func.arch, v)
                if slot is not None and slot not in out:
                    out[slot] = v
        except Exception:
            pass
        return out

    def local_slot(arch, var):
        try:
            if var.source_type != VariableSourceType.RegisterVariableSourceType:
                return None
            name = arch.get_reg_name(var.storage)
        except Exception:
            return None
        if name.startswith("l") and name[1:].split("_")[0].isdigit():
            return int(name[1:].split("_")[0])
        return None

    def header_text(func):
        """Java declaration of the function, e.g. 'public static int parse(String text, int p2)'"""
        info = _ClassInfo.get(func.view)
        m = info.method_at(func.start)
        if m is None:
            return None
        name, desc, flags = m
        slots = arg_slots(desc, bool(flags & ACC_STATIC))
        by_slot = param_vars_by_slot(func)
        names = [None] * len(slots)
        for slot, (i, _) in slots.items():
            v = by_slot.get(slot)
            names[i] = java_var_name(v.name) if v is not None else "p%d" % (i + 1)
        return method_header(info.class_name or "?", name, desc, flags, names)

    class PseudoJavaFunctionType(LanguageRepresentationFunctionType):
        language_name = LANGUAGE_NAME

        def create(self, arch, owner, hlil):
            return PseudoJavaFunction(self, arch, owner, hlil)

        def is_valid(self, view):
            try:
                if view.arch is not None:
                    return view.arch.name == ARCH_NAME
                return view.view_type == VIEW_NAME
            except Exception:
                return False

        def function_type_tokens(self, func, settings):
            text = header_text(func)
            if text is None:
                return []
            tokens = []
            words = text.split(" ")
            head, _, rest = text.partition("(")
            pre = head.rsplit(" ", 1)
            for w in (pre[0].split(" ") if len(pre) > 1 else []):
                kind = TT.KeywordToken if w in KEYWORDS else TT.TypeNameToken
                tokens.append(_tok(kind, w))
                tokens.append(_tok(TT.TextToken, " "))
            tokens.append(_tok(TT.CodeSymbolToken, pre[-1], value=func.start))
            if "(" in text:
                tokens.append(_tok(TT.BraceToken, "("))
                tokens.append(_tok(TT.TextToken, rest[:-1]))
                tokens.append(_tok(TT.BraceToken, ")"))
            return [DisassemblyTextLine(tokens, func.start)]

    KEYWORDS = {"public", "private", "protected", "abstract", "static", "final", "synchronized", "native",
                "strictfp", "void", "int", "long", "short", "byte", "char", "boolean", "float", "double"}

    _registered = None

    def register():
        global _registered
        if _registered is None:
            if LANGUAGE_NAME in LanguageRepresentationFunctionType:
                _registered = LanguageRepresentationFunctionType[LANGUAGE_NAME]
            else:
                _registered = PseudoJavaFunctionType()
                _registered.register()
        return _registered

    def representation(func):
        """the Pseudo Java LanguageRepresentationFunction of func"""
        try:
            lr = func.language_representation(LANGUAGE_NAME)
            if lr is not None:
                return lr
        except Exception:
            pass
        return PseudoJavaFunction(register(), func.arch, func, func.hlil)

    def render_body(func):
        """the method body only: no header, no outer braces, dedented"""
        lines = render_method(func)
        if lines and lines[0] == header_text(func):
            lines = lines[1:]
        if lines and lines[0].strip() == "{" and lines[-1].strip() == "}":
            lines = lines[1:-1]
        indent = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
        return [l[indent:] for l in lines]

    def render_method(func):
        """Java header + body of func as plain text lines (for the class view / reports)."""
        lines = []
        header = header_text(func)
        if header:
            lines.append(header)
        hlil = func.hlil
        if hlil is None or hlil.root is None:
            why = "analysis skipped: %s" % func.analysis_skip_reason.name if func.analysis_skipped else "no HLIL"
            return lines + ["{", "    // " + why, "}"]
        lr = representation(func)
        for line in lr.get_linear_lines(hlil.root):
            lines.append("".join(t.text for t in line.tokens).rstrip())
        return lines
else:
    def register():
        return None

    def render_method(func):
        raise RuntimeError("Pseudo Java needs Binary Ninja")

    def render_body(func):
        raise RuntimeError("Pseudo Java needs Binary Ninja")
