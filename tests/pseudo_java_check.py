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


check("lang JVM", pj.language_name_for("JVM"), "Pseudo-Java")
check("lang dev", pj.language_name_for("JVM-dev3"), "Pseudo-Java JVM-dev3")
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

if failures:
    print("\n".join(failures))
    print("%d failure(s)" % len(failures))
    sys.exit(1)
print("pseudo_java offline checks: ok")
