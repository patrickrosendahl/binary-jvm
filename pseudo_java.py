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
"""Pseudo-Java: a language representation that prints JVM-method HLIL as Java-like source.

What it does on top of plain HLIL (all at render time, nothing is rewritten in the IL):
  * calls through constant-pool slots print as `obj.m(x)` (receiver = first argument) or `Foo.m(x)`
    (static: the call has exactly the descriptor's arguments)
  * object intrinsics: `obj.f`, `obj.f = v`, `new T[n]`, `(T) x`, `x instanceof T`, `a.length`, `throw x`,
    `// synchronized (o) {` markers; static fields `Foo.f`; pool strings as Java string literals
  * idioms (jvm-44): `v = new(X); X.<init>(v, a)` -> `X v = new X(a)`, StringBuilder / StringBuffer
    append chains and invokedynamic makeConcatWithConstants -> `a + b`, boxing `Integer.valueOf(x)` /
    `x.intValue()` -> `x` (HIDE_BOXING), `<init>`/`<clinit>` -> constructor / `static {}`, `super(...)`
  * try/catch from the method's exception table (`jvm.exception_table` function metadata, else the
    class file): runs of statements inside a try range get wrapped in `try { } catch (T e) { }`; the
    exception plumbing of the lifter (`exc` register, `if (exc != 0)` checks, handler type tests) is
    never printed as such.

`render_method(func) -> list[str]` returns the Java header plus body lines (used by the class view).
"""
from .constants import (ARCH_NAME, VIEW_NAME, PSEUDOMEMORY_TABLE, PSEUDOMEMORY_PRIMITIVES, POOL_STRIDE,
                        METHOD_BASE, METHOD_STRIDE)

# ---------------------------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------------------------
HIDE_BOXING = True          # Integer.valueOf(x) / x.intValue() -> x
QUALIFY_OWN_STATICS = False  # print `m(x)` instead of `ThisClass.m(x)` for static calls into the own class
SIMPLE_CLASS_NAMES = True    # java.lang.String -> String


def language_name_for(arch_name):
    """'JVM' -> 'Pseudo-Java'; dev loads ('JVM-dev3') get their own name, since BN can't unregister one."""
    if arch_name == "JVM":
        return "Pseudo-Java"
    return "Pseudo-Java " + arch_name


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
    """Exception-table entries [[start, end, handler, type], ...] -> [(start, end, [(handler, type), ...])]
    grouped by protected range, in table order (javac emits inner tries first)."""
    groups = []
    index = {}
    for start, end, handler, ctype in table:
        key = (start, end)
        if key not in index:
            index[key] = len(groups)
            groups.append((start, end, []))
        groups[index[key]][2].append((handler, ctype))
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
            self.placed = set()
            self.hoisted = set()
            self.active_tries = []
            self.caught = []
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
                if i.operation in (Op.HLIL_VAR, Op.HLIL_VAR_SSA) and self.is_exc_var(i.var):
                    return True
                if i.operation in (Op.HLIL_VAR_INIT, Op.HLIL_VAR_DECLARE):
                    if self.is_exc_var(i.dest if i.operation == Op.HLIL_VAR_INIT else i.var):
                        return True
            return False

        def is_exc_plumbing(self, s):
            """statements of the lifter's exception model that have no Java counterpart"""
            o = s.operation
            if o == Op.HLIL_ASSIGN and s.dest.operation == Op.HLIL_VAR and self.is_exc_var(s.dest.var):
                return True  # exc = 0
            if o == Op.HLIL_VAR_INIT and self.is_exc_var(s.dest):
                return True
            if o == Op.HLIL_INTRINSIC and s.intrinsic.name.startswith("__") and self.mentions_exc(s):
                return True  # __propagate(exc)
            if o == Op.HLIL_IF and s.false.operation in (Op.HLIL_NOP,) and self.mentions_exc(s.condition) \
                    and s.true.operation in (Op.HLIL_GOTO, Op.HLIL_NORET, Op.HLIL_UNREACHABLE):
                return True  # if (exc != 0) goto handler
            return False

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
            if instr.expr_index == self.hlil.root.expr_index:
                self.placed = set()
                self.hoisted = set()
            body = list(instr.body)
            plan = self.plan_block(body)
            self.emit_range(body, 0, len(body) - 1, plan, tokens, settings, instr)

        def emit_range(self, body, lo, hi, plan, tokens, settings, block):
            """emit body[lo..hi], wrapping the try regions that fit (not already open further out)"""
            runs = []
            if self.try_groups and block.as_ast:
                start = self.function.start
                pcs = []
                for s in body[lo:hi + 1]:
                    if s.operation == Op.HLIL_LABEL or self.is_exc_plumbing(s) or \
                            (s.operation == Op.HLIL_IF and self.mentions_exc(s.condition)):
                        pcs.append(None)
                    else:
                        pcs.append([s.address - start])
                # a try range is printed once per function, where it first fits
                runs = try_runs(pcs, self.try_groups, {(g[0], g[1]) for g in self.active_tries} | self.placed)
            run_at = {r[0] + lo: (r[0] + lo, r[1] + lo, r[2]) for r in runs}
            for r in runs:
                self.placed.add((r[2][0], r[2][1]))
            need_separator = None  # None: nothing emitted yet in this range
            idx = lo
            while idx <= hi:
                if idx in run_at:
                    first, last, group = run_at[idx]
                    if need_separator is not None:
                        tokens.scope_separator()
                    self.emit_try(body, first, last, group, plan, tokens, settings, block)
                    idx = last + 1
                    need_separator = True
                    continue
                need_separator = self.emit_statement(body, idx, plan, tokens, settings, block, need_separator)
                idx += 1

        def emit_statement(self, body, idx, plan, tokens, settings, block, need_separator):
            s = body[idx]
            if idx in plan["skip"] or self.is_exc_plumbing(s):
                return need_separator
            if (block.as_ast and idx + 1 == len(body) and s.operation == Op.HLIL_RET and
                    (len(s.src) == 0 or (self.returns_void and not self.void_return_call(s)))
                    and block.expr_index == self.hlil.root.expr_index):
                return need_separator
            if self.is_implicit_super(s) or s.operation == Op.HLIL_NORET:
                return need_separator
            call = self.void_return_call(s) if s.operation == Op.HLIL_RET and self.returns_void else None
            if call is not None:
                # `return f()` in a void method: f() is a statement (the value is the lifter's r register)
                last = idx + 1 == len(body) and block.expr_index == self.hlil.root.expr_index
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
            if self.active_tries and s.operation == Op.HLIL_IF and self.mentions_exc(s.condition):
                # if (exc != 0) { handler } else { rest }: the handler becomes the catch body
                hb = s.true
                if hb.operation not in (Op.HLIL_GOTO, Op.HLIL_NOP, Op.HLIL_NORET, Op.HLIL_UNREACHABLE):
                    self.caught[-1].setdefault(hb.address - self.function.start, hb)
                if s.false is not None and s.false.operation not in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE):
                    self.perform_get_expr_text(s.false, tokens, settings, P.TopLevelOperatorPrecedence, True)
                return need_separator
            has_blocks = s.operation in COMPOUND
            if need_separator or (need_separator is not None and has_blocks):
                tokens.scope_separator()
            if idx in plan["new_at"]:
                self.emit_new(plan["new_at"][idx], tokens, settings)
                tokens.append_semicolon()
            else:
                self.perform_get_expr_text(s, tokens, settings, P.TopLevelOperatorPrecedence, True)
                if self.needs_semicolon(s):
                    tokens.append_semicolon()
            tokens.new_line()
            return has_blocks

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
            self.caught.append({})  # handler pc -> HLIL body found in `if (exc != 0)` checks
            # variables declared in the try block but used after it are declared before it
            for k in range(first, last + 1):
                s = body[k]
                if s.operation == Op.HLIL_VAR_INIT and k not in plan["skip"] and s.dest not in self.hoisted:
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
            self.emit_range(body, first, last, plan, tokens, settings, block)
            tokens.end_scope(ScopeType.BlockScopeType)
            caught = self.caught.pop()
            self.active_tries.pop()
            # handler bodies that follow the try run in this block (javac layout: try; goto end; handler)
            following = {}
            pos = last + 1
            for hpc, _ in handlers:
                for k in range(pos, len(body)):
                    if body[k].address - self.function.start == hpc:
                        following[hpc] = k
                        break
            for n, (hpc, ctype) in enumerate(handlers):
                tokens.scope_continuation(False)
                if ctype:
                    self.kw(tokens, "catch ")
                    tokens.append_open_paren()
                    self.type_tok(tokens, java_class_name(ctype))
                    self.txt(tokens, " e")
                    tokens.append_close_paren()
                else:
                    self.kw(tokens, "finally")  # javac's catch-any: finally (or synchronized cleanup)
                tokens.begin_scope(ScopeType.BlockScopeType)
                if hpc in caught:
                    self.emit_body(caught[hpc], tokens, settings)
                elif hpc in following:
                    # statements from the handler start up to the next handler / label
                    k = following[hpc]
                    stop = min([v for v in following.values() if v > k] + [len(body)])
                    for kk in range(k, stop):
                        if body[kk].operation == Op.HLIL_LABEL and kk != k:
                            break
                        self.emit_statement(body, kk, plan, tokens, settings, block, None)
                        plan["skip"].add(kk)
                else:
                    self.note(tokens, "// handler at pc %#x%s" % (hpc, self.handler_hint(hpc)))
                    tokens.new_line()
                tokens.end_scope(ScopeType.BlockScopeType)
            tokens.finalize_scope()
            tokens.new_line()

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

        def _cond(self, instr, tokens, settings):
            tokens.append_open_paren()
            self.expr(instr, tokens, settings)
            tokens.append_close_paren()

        def _expr_text(self, instr, tokens, settings, precedence, statement):
            o = instr.operation
            if o == Op.HLIL_BLOCK:
                self.emit_block(instr, tokens, settings)
            elif o == Op.HLIL_IF:
                if self.mentions_exc(instr.condition):
                    # exception check (or handler type dispatch) outside a placed try region: no `exc` in
                    # the output, the exceptional path becomes a commented block
                    self.note(tokens, "// may throw -> catch%s" % self.catch_hint(instr))
                    if not instr.as_ast:
                        return
                    if instr.true.operation not in (Op.HLIL_GOTO, Op.HLIL_NOP, Op.HLIL_NORET, Op.HLIL_UNREACHABLE) \
                            and not self.is_exc_plumbing(instr.true):
                        tokens.new_line()
                        self.note(tokens, "// on exception:")
                        self._scoped(instr, instr.true, tokens, settings)
                        tokens.finalize_scope()
                    if instr.false is not None and instr.false.operation not in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE):
                        tokens.new_line()
                        self.perform_get_expr_text(instr.false, tokens, settings, P.TopLevelOperatorPrecedence, True)
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
                self._cond(instr.condition, tokens, settings)
                if instr.as_ast:
                    self._scoped(instr, instr.body, tokens, settings)
                    tokens.finalize_scope()
            elif o == Op.HLIL_DO_WHILE:
                if instr.as_ast:
                    self.kw(tokens, "do")
                    self._scoped(instr, instr.body, tokens, settings)
                    tokens.scope_continuation(True)
                    self.kw(tokens, "while ")
                    self._cond(instr.condition, tokens, settings)
                    tokens.append_semicolon()
                    tokens.finalize_scope()
                else:
                    self.kw(tokens, "do while ")
                    self._cond(instr.condition, tokens, settings)
            elif o == Op.HLIL_FOR:
                self.kw(tokens, "for ")
                tokens.append_open_paren()
                if instr.init.operation != Op.HLIL_NOP:
                    self.expr(instr.init, tokens, settings)
                self.txt(tokens, "; ")
                if instr.condition.operation != Op.HLIL_NOP:
                    self.expr(instr.condition, tokens, settings)
                self.txt(tokens, "; ")
                if instr.update.operation != Op.HLIL_NOP:
                    self.expr(instr.update, tokens, settings)
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
                tokens.append(_tok(TT.GotoLabelToken, instr.target.name, instr.target.label_id))
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
            tokens.append(_tok(TT.GotoLabelToken, instr.target.name, instr.target.label_id))
            self.txt(tokens, ":")

        def emit_var(self, var, instr, tokens):
            if self.this_var is not None and var == self.this_var:
                tokens.append(_tok(TT.KeywordToken, "this"))
                return
            if self.is_exc_var(var):
                tokens.append(_tok(TT.LocalVariableToken, "e", context=InstructionTextTokenContext.LocalVariableTokenContext,
                                   address=instr.expr_index, value=var.identifier, size=instr.size))
                return
            tokens.append(_tok(TT.LocalVariableToken, java_var_name(var.name), address=instr.expr_index,
                               size=instr.size, value=var.identifier,
                               context=InstructionTextTokenContext.LocalVariableTokenContext))

        def emit_var_decl(self, var, instr, tokens, src=None):
            # the initializer's type (descriptors, casts, `new`) beats the variable's propagated BN type: the
            # stack registers are shared by unrelated values, so BN's type for them is often a neighbour's
            type_name = self.expr_java_type(src) if src is not None else None
            if type_name is None:
                code = self.var_code(var)  # e.g. `i = 0` ... `i++`: an int whatever BN propagated
                type_name = PRIMITIVES.get(code) if code and code in "BCDFIJSZ" else java_type_of(var.type)
            self.type_tok(tokens, type_name)
            self.txt(tokens, " ")
            tokens.append(_tok(TT.LocalVariableToken, java_var_name(var.name), address=instr.expr_index,
                               size=instr.size, value=var.identifier,
                               context=InstructionTextTokenContext.LocalVariableTokenContext))
            return type_name

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
            if name == "athrow" and len(p) == 1:
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
        """the Pseudo-Java LanguageRepresentationFunction of func"""
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
        raise RuntimeError("Pseudo-Java needs Binary Ninja")

    def render_body(func):
        raise RuntimeError("Pseudo-Java needs Binary Ninja")
