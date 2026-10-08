"""Offline checks for the pure-Python parts of pseudo_java.py (no Binary Ninja licence needed).

Stubs `binaryninja` the same way tests/offline_lift_check.py does, loads the plugin package and checks
descriptor parsing, method headers, string literals, the indy concat recipe split and try-region placement.
The HLIL printer itself is only testable inside Binary Ninja (tests/bn_pseudo_java.py).

usage: python3 tests/pseudo_java_check.py
"""
import importlib.util, os, sys, types


class _Dummy:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name): return _Dummy()
    def __call__(self, *a, **k): return _Dummy()


class _DummyMeta(type):
    def __getitem__(cls, key): return _Dummy()
    def __getattr__(cls, name): return _Dummy()


class _DummyClass(metaclass=_DummyMeta):
    def __init__(self, *a, **k): pass


def _stub_attr(name):
    if name == "LLIL_TEMP":
        return lambda n: 0x80000000 | n
    return type(name, (_DummyClass,), {})


bn = types.ModuleType("binaryninja")
bn.__getattr__ = _stub_attr
sys.modules.setdefault("binaryninja", bn)
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_spec = importlib.util.spec_from_file_location("binary_jvm", os.path.join(ROOT, "__init__.py"),
                                               submodule_search_locations=[ROOT])
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["binary_jvm"] = _pkg
_spec.loader.exec_module(_pkg)
from binary_jvm import pseudo_java as pj

failures = []


def check(name, got, want):
    if got != want:
        failures.append("%s: got %r, want %r" % (name, got, want))


check("lang JVM", pj.language_name_for("JVM"), "Pseudo Java")
check("lang dev", pj.language_name_for("JVM-dev3"), "Pseudo Java JVM-dev3")
check("class simple", pj.java_class_name("java/lang/String"), "String")
check("class full", pj.java_class_name("java/lang/String", False), "java.lang.String")
check("inner", pj.java_class_name("com/x/Outer$Inner"), "Outer.Inner")
check("anon", pj.java_class_name("com/x/Outer$1"), "Outer$1")
check("array class", pj.java_class_name("[Ljava/lang/Object;"), "Object[]")
check("descriptor", pj.descriptor_types("(I[JLjava/lang/String;[[Ljava/util/Map;)V"),
      (["int", "long[]", "String", "Map[][]"], "void"))
check("descriptor ret", pj.descriptor_types("()[Ljava/lang/String;"), ([], "String[]"))
check("arg codes", pj.descriptor_arg_codes("(ILjava/lang/String;[JD)V"), ["I", "L", "[", "D"])
check("header main", pj.method_header("a/Main", "main", "([Ljava/lang/String;)V", 0x0009, ["args"]),
      "public static void main(String[] args)")
check("header ctor", pj.method_header("com/x/Foo", "<init>", "(IZ)V", 0x0001),
      "public Foo(int p1, boolean p2)")
check("header clinit", pj.method_header("com/x/Foo", "<clinit>", "()V", 0x0008), "static")
check("header varargs", pj.method_header("a/B", "f", "([Ljava/lang/Object;)Ljava/lang/String;", 0x0081, ["xs"]),
      "public String f(Object... xs)")
check("header sync", pj.method_header("a/B", "g", "(J)J", 0x0022, ["n"]), "private synchronized long g(long n)")
check("string", pj.java_string_literal('a"b\\c\n\x01'), '"a\\"b\\\\c\\n\\u0001"')
check("char", pj.java_string_literal("'", "'"), "'\\''")
check("recipe", pj.concat_recipe_parts("x=\x01, y=\x01!", ["A", "B"]),
      [("lit", "x="), ("arg", "A"), ("lit", ", y="), ("arg", "B"), ("lit", "!")])
check("recipe const", pj.concat_recipe_parts("\x02\x01", ["A"], ["K"]), [("lit", "K"), ("arg", "A")])

table = [[0, 10, 20, "java/io/IOException"], [0, 10, 30, ""], [2, 6, 40, "java/lang/RuntimeException"]]
groups = pj.group_try_entries(table)
check("groups", groups, [(0, 10, [(20, "java/io/IOException"), (30, "")]),
                         (2, 6, [(40, "java/lang/RuntimeException")])])
# javac shapes (jvm-42 tables): try/catch/finally of PropLoader.collectProperties, incl. the jsr subroutine's own try
check("groups catch+finally", pj.group_try_entries(
    [[2, 162, 168, "java/lang/Exception"], [2, 165, 206, ""], [168, 203, 206, ""], [206, 211, 206, ""],
     [220, 224, 227, "java/lang/Exception"]]),
    [(2, 165, [(168, "java/lang/Exception"), (206, "")]), (220, 224, [(227, "java/lang/Exception")])])
check("groups split finally", pj.group_try_entries([[0, 5, 20, ""], [8, 12, 20, ""], [20, 22, 20, ""]]),
      [(0, 12, [(20, "")])])
check("groups catch inside try-finally", pj.group_try_entries([[2, 6, 8, "E"], [0, 14, 20, ""], [20, 22, 20, ""]]),
      [(0, 14, [(20, "")]), (2, 6, [(8, "E")])])
check("groups multi-catch", pj.group_try_entries([[0, 10, 12, "A"], [0, 10, 20, "B"]]),
      [(0, 10, [(12, "A"), (20, "B")])])
check("groups two catches + finally", pj.group_try_entries(
    [[0, 10, 12, "A"], [0, 10, 20, "B"], [0, 11, 30, ""], [12, 18, 30, ""], [20, 26, 30, ""], [30, 32, 30, ""]]),
    [(0, 11, [(12, "A"), (20, "B"), (30, "")])])
check("groups self-covering only", pj.group_try_entries([[5, 9, 12, ""], [12, 15, 12, ""]]), [(5, 9, [(12, "")])])
# statements at pcs 0, 3, 8, 12 (outside), 20 (handler)
pcs = [[0], [3], [8], [12], [20]]
check("runs outer", pj.try_runs(pcs, groups), [(0, 2, groups[0])])
check("runs active", pj.try_runs(pcs, groups, {(0, 10)}), [(1, 1, groups[1])])
check("runs none", pj.try_runs([[12], [20]], groups), [])
check("runs unknown pcs", pj.try_runs([None, [3], [4]], groups), [(1, 2, groups[0])])
check("runs neutral inside", pj.try_runs([[3], None, [4], None, [12]], groups), [(0, 2, groups[0])])
check("slots static", pj.arg_slots("(IJLjava/lang/String;D)V", True), {0: (0, "I"), 1: (1, "J"), 3: (2, "L"), 4: (3, "D")})
check("slots instance", pj.arg_slots("(Z)V", False), {1: (0, "Z")})
check("var name", pj.java_var_name("com/is_teledata/mdg/MDGAttributeDefinition.LOGGER_1"), "MDGAttributeDefinition_LOGGER_1")
check("int lit", [pj.java_int_literal(0xffffffff, 4), pj.java_int_literal(0x7fffffff, 4), pj.java_int_literal(5, 8)],
      ["-1", "0x7fffffff", "5L"])
check("register under stub", pj.register(), None)
# field initialisers (jvm-52): common prefix of the constructors, declaration order, delegating ctors skipped
fields = [("a", False), ("b", False), ("c", False), ("K", True), ("L", True)]
inits, ctors, clinit = pj.hoist_field_initializers(
    "Foo", fields,
    [["this.a = new Hashtable();", "this.b = 5;", "this.c = arg1;", "f();"],
     ["super(x);", "this.a = new Hashtable();", "this.b = 5;", "this.c = 0;"],
     ["this(1);", "g();"]],
    ["Foo.K = \"\";", "Foo.L = Level.DEBUG;", "h();"])
check("hoist inits", inits, {"a": "new Hashtable()", "b": "5", "K": '""', "L": "Level.DEBUG"})
check("hoist ctors", ctors, [["this.c = arg1;", "f();"], ["super(x);", "this.c = 0;"], ["this(1);", "g();"]])
check("hoist clinit", clinit, ["h();"])
inits, ctors, _ = pj.hoist_field_initializers("Foo", fields, [["this.b = 1;", "this.a = 2;"]])
check("hoist order", (inits, ctors), ({"b": "1"}, [["this.a = 2;"]]))
inits, ctors, _ = pj.hoist_field_initializers("Foo", fields, [["this.a = arg1.x();"]])
check("hoist not simple", inits, {})
check("constant literals", [pj.constant_literal("int", 0xffffffff, "I"), pj.constant_literal("int", 1, "Z"),
                            pj.constant_literal("int", 65, "C"), pj.constant_literal("long", (1 << 64) - 2, "J"),
                            pj.constant_literal("float", 1.5, "F"), pj.constant_literal("double", float("inf"), "D"),
                            pj.constant_literal("string", 'a"b', "Ljava/lang/String;")],
      ["-1", "true", "'A'", "-2L", "1.5f", "Double.POSITIVE_INFINITY", '"a\\"b"'])

# names for auto-named variables (jvm-55)
check("auto names", [bool(pj.AUTO_VAR_NAME.match(n)) for n in
                     ["st0_lo", "st1_lo_3", "st0_1", "r", "r_12", "r64", "r64_3", "l3_lo", "l2_lo_1", "arg2", "result",
                      "i", "e", "XidProducer_LOGGER_1", "rawValue"]],
      [True] * 9 + [False] * 6)
check("name from method", [pj.name_from_method(m) for m in
                           ["getRawValue", "getXidObject", "toLowerCase", "nextElement", "elementAt", "isOpen", "size",
                            "LOGGER", "jE", "toString", "<init>", "getClass", "hasMoreElements", "get", "a"]],
      ["rawValue", "xidObject", "lowerCase", "element", "element", "open", "size", "logger", "jE", None, None,
       None, "moreElements", "get", None])
check("name from type", [pj.name_from_type(t) for t in
                         ["Vector", "StatsItem", "String", "int", "long[]", "Map.Entry", "byte[]", "Property",
                          "java.util.Hashtable", "Outer$Inner", None, "Object[]"]],
      ["vector", "statsItem", "str", "n", "longs", "entry", "bytes", "property", "hashtable", "inner", None,
       "objects"])
taken = {"str", "arg2"}
check("unique names", [pj.unique_name("str", taken), pj.unique_name("str", taken), pj.unique_name("vector", taken)],
      ["str2", "str3", "vector"])

# varargs calls (jvm-57)
SCHED = os.path.join(ROOT, "sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg/com/is_teledata/util/Scheduler.class")
if os.path.exists(SCHED):
    flags = pj.class_method_flags(open(SCHED, "rb").read())
    check("method flags", flags.get(("addJob", "(Lcom/is_teledata/util/Producer;J)Ljava/lang/Long;")), 0x11)
    check("method flags count", len(flags), 7)
check("method flags junk", pj.class_method_flags(b"nope"), None)
GC = "([Ljava/lang/Class;)Ljava/lang/reflect/Constructor;"
check("varargs spread", [pj.varargs_call_args(GC, 1, ["value"], None),
                         pj.varargs_call_args(GC, 1, ["value", "value"], None),
                         pj.varargs_call_args(GC, 1, [], None),
                         pj.varargs_call_args(GC, 1, ["null"], None),
                         pj.varargs_call_args(GC, 1, ["array"], None),
                         pj.varargs_call_args("(I[Ljava/lang/Object;)V", 2, ["value"], ["(II)V"]),
                         pj.varargs_call_args("(I[Ljava/lang/Object;)V", 2, ["value", "null"], ["(II)V"]),
                         pj.varargs_call_args("(Ljava/lang/Object;)V", 1, ["value"], None)],
      [True, True, True, False, False, False, True, False])

# common supertypes for declared types (jvm-58)
sup = {"A": ["Base", "Iface"], "B": ["Base"], "Base": ["Object"], "C": ["Object", "Iface"]}.get
check("common supertype", [pj.common_supertype(t, sup) for t in
                           [["String", "String"], ["String", "SubscriptionFilter"], ["A", "B"], ["A", "C"],
                            ["int", "short"], ["int", "long"], ["int", "String"], ["boolean", "int"],
                            [None, "String"], [], ["String[]", "Object[]"], ["int[]", "long[]"], ["int[]", "String"],
                            ["IOException", "RuntimeException"], ["Vector", "ArrayList"], ["Integer", "Long"]]],
      ["String", "Object", "Base", "Iface", "int", "long", None, None, "String", None, "Object[]", "Object", "Object",
       "Exception", "AbstractList", "Number"])
if os.path.exists(SCHED):
    check("class supertypes", pj.class_supertypes(open(SCHED, "rb").read()), ("java/lang/Thread", []))
check("class supertypes junk", pj.class_supertypes(b"nope"), None)

# condition graphs (jvm-80): strings stand in for HLIL conditions
def cg(nodes, root):
    graph = dict(nodes)
    top = pj.cg_reduce(nodes, root)
    return top, pj.cg_verify(graph, root, top)

A, B = ('leaf', 'A', []), ('leaf', 'B', [])
# if (p) { if (q) A else B } else B  ->  p && q
top, ok = cg({0: A, 1: B, 2: ('dec', 'q', 0, 1), 3: ('dec', 'p', 2, 1)}, 3)
check("cg and", (top[1], top[2:], ok), (('and', 'p', 'q'), (0, 1), True))
# if (p) A else { if (q) A else B }  ->  p || q
top, ok = cg({0: A, 1: B, 2: ('dec', 'q', 0, 1), 3: ('dec', 'p', 0, 2)}, 3)
check("cg or", (top[1], top[2:], ok), (('or', 'p', 'q'), (0, 1), True))
# if (p) { if (q) A else B } else { if (r) A else B }  ->  p ? q : r
top, ok = cg({0: A, 1: B, 2: ('dec', 'q', 0, 1), 3: ('dec', 'r', 0, 1), 4: ('dec', 'p', 2, 3)}, 4)
check("cg tern", (top[1], top[2:], ok), (('tern', 'p', 'q', 'r'), (0, 1), True))
# swapped exits on one side: p ? q : !r
top, ok = cg({0: A, 1: B, 2: ('dec', 'q', 0, 1), 3: ('dec', 'r', 1, 0), 4: ('dec', 'p', 2, 3)}, 4)
check("cg tern swap", (top[1], top[2:], ok), (('tern', 'p', 'q', ('not', 'r')), (0, 1), True))
# test7's shape: ((c ? x : y) ? (u ? v : w) : (s ? t : z)) over two shared outcomes
top, ok = cg({0: A, 1: B, 2: ('dec', 'v', 0, 1), 3: ('dec', 'w', 0, 1), 4: ('dec', 'u', 2, 3),
              5: ('dec', 't', 0, 1), 6: ('dec', 'z', 0, 1), 7: ('dec', 's', 5, 6),
              8: ('dec', 'x', 4, 7), 9: ('dec', 'y', 4, 7), 10: ('dec', 'c', 8, 9)}, 10)
check("cg nested", (top[1], top[2:], ok),
      (('tern', ('tern', 'c', 'x', 'y'), ('tern', 'u', 'v', 'w'), ('tern', 's', 't', 'z')), (0, 1), True))
# three outcomes: stays a graph of decisions
C = ('leaf', 'C', [])
top, ok = cg({0: A, 1: B, 2: C, 3: ('dec', 'q', 0, 1), 4: ('dec', 'p', 3, 2)}, 4)
check("cg three outcomes", top, ("dec", "p", 3, 2))  # not reducible: cg_verify only judges two outcomes
# the verifier rejects a wrong reduction
check("cg verify wrong", pj.cg_verify({0: A, 1: B, 2: ('dec', 'q', 0, 1), 3: ('dec', 'p', 2, 1)}, 3,
                                      ('dec', ('or', 'p', 'q'), 0, 1)), False)

# javac's Java 8 desugaring (jvm-44): tests/synthetic/idioms (Idioms.java, compiled --release 8)
IDIOMS = os.path.join(ROOT, "tests/synthetic/idioms/classes/jvmtest")
idioms = open(os.path.join(IDIOMS, "Idioms.class"), "rb").read()
shapes = {}
for (name, desc), _ in pj.class_methods_code(idioms)[1].items():
    if name.startswith("access$"):
        a = pj.accessor_shape(idioms, name, desc)
        shapes[name] = a and (a["kind"], a["static"], a["pre"], a["op"], a["member"][1])
check("accessors", sorted(shapes.values(), key=str), sorted([
    ("get", False, None, None, "i"), ("put", False, None, None, "i"), ("inc", False, False, "+", "i"),
    ("inc", True, True, "-", "s"), ("compound", False, None, "|", "i"), ("call", False, None, None, "priv"),
    ("get", True, None, None, "s")], key=str))
check("switch map", pj.enum_switch_map(open(os.path.join(IDIOMS, "Idioms$1.class"), "rb").read(),
                                       "$SwitchMap$java$util$concurrent$TimeUnit"), {1: "SECONDS", 2: "MINUTES"})

if failures:
    print("\n".join(failures))
    print("%d failure(s)" % len(failures))
    sys.exit(1)
print("pseudo_java offline checks: ok")
