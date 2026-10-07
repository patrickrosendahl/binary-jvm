# Run inside Binary Ninja via the script bridge, after tests/bn_dev_load.py:
#   bnrun --parallel --timeout 400 tests/bn_pseudo_java_compare.py
#   (prepend DEV_PKG = "jvm_devN", CLASSES = [...], SHOW = True to print the Java of failing methods,
#    ONLY_EXC = True to check only methods with an exception table)
# Renders every method through Pseudo Java and checks it against the HLIL:
#   * nothing lost: every method called in HLIL (by name) appears in the Java text, and every string
#     literal too
#   * no exception plumbing: no `exc`, `__exception`, `__propagate`, `instanceof(`, no render errors
#   * not truncated: balanced braces, last line `}`
#   * a method with an exception table has `try` (except javac's synchronized-only cleanup handlers)
#   * no catch variable used outside its catch block, no jump right after a jump (`return; break;`) (jvm-45)
import os, re, sys, time
import binaryninja as b
sys.path.insert(0, "/Users/patrick/dev/binary-jvm/tests")
import importlib, vineflower_compare as _vc
_vc = importlib.reload(_vc)  # the app's interpreter keeps modules between bnrun calls

CLASS_DIR = globals().get("CLASS_DIR", "/Users/patrick/dev/binary-jvm/sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")
CLASSES = globals().get("CLASSES", ["com/is_teledata/cache/HashCache.class"])
SHOW = globals().get("SHOW", False)
ONLY_EXC = globals().get("ONLY_EXC", False)
PKG = globals().get("DEV_PKG") or b._jvm_dev["pkg"].__name__
pj = sys.modules[PKG + ".pseudo_java"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
Op = b.HighLevelILOperation
SKIP_NAMES = {"<init>", "append", "toString", "valueOf", "intValue", "longValue", "booleanValue", "doubleValue",
              "floatValue", "shortValue", "byteValue", "charValue", "makeConcatWithConstants"}
PLUMBING = [r"\bexc(_\d+)?\b", r"__exception", r"__propagate", r"\binstanceof\(", r"could not render"]

totals = {"methods": 0, "with_table": 0, "ok": 0, "problems": 0}
for rel in CLASSES:
    t = time.time()
    v = vt.create(b.BinaryView.open(os.path.join(CLASS_DIR, rel)))
    v.update_analysis_and_wait()
    skipped = [f for f in v.functions if f.analysis_skipped]
    if skipped:
        for f in skipped:
            f.analysis_skip_override = b.FunctionAnalysisSkipOverride.NeverSkipFunctionAnalysis
        v.update_analysis_and_wait()
    info = pj._ClassInfo.get(v)
    for f in sorted(v.functions, key=lambda f: f.start):
        table = info.exception_table(f)
        if ONLY_EXC and not table:
            continue
        totals["methods"] += 1
        totals["with_table"] += bool(table)
        problems = []
        try:
            lines = pj.render_method(f)
        except Exception as e:
            lines = []
            problems.append("render raised %r" % e)
        text = "\n".join(lines)
        flat = re.sub(r'"\s*\n\s*"', "", text)  # long string literals wrap across lines
        if f.hlil is None or f.hlil.root is None:
            continue
        # calls and strings in HLIL
        names, strings = set(), set()
        lr = pj.PseudoJavaFunction(pj.register(), f.arch, f, f.hlil)
        lr._setup()
        for i in pj._walk(f.hlil.root):
            shape = None
            if i.operation == Op.HLIL_CALL or (i.operation == Op.HLIL_INTRINSIC and i.intrinsic.name.startswith("invoke")):
                shape = lr.call_shape(i)
                if shape is None and i.operation == Op.HLIL_CALL:
                    m = re.match(r"sub_([0-9a-f]+)\(", str(i))
                    if m and int(m.group(1), 16) not in lr.finally_subs():  # jsr finally bodies print inline
                        names.add("sub_" + m.group(1))
            if shape and shape[2] not in SKIP_NAMES:
                names.add(shape[2])
            if i.operation in (Op.HLIL_CONST_PTR, Op.HLIL_CONST):
                idx = pj._pool_index(i.constant)
                s = info.string(idx) if idx is not None else None
                if s is not None:
                    strings.add(s)
        for n in sorted(names):
            if not re.search(r"\b%s\(" % re.escape(n), text):
                problems.append("call lost: %s" % n)
        for s in sorted(strings):
            if pj.java_string_literal(s)[1:-1] not in flat:
                problems.append("string lost: %r" % s[:40])
        for pat in PLUMBING:
            for m in re.finditer(pat, text):
                problems.append("plumbing: %r" % text[max(0, m.start() - 30):m.end() + 10].replace("\n", " "))
                break
        # braces outside comments and string literals
        code = re.sub(r'"(\\.|[^"\\])*"', '""', text)
        code = re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", code))
        if code.count("{") != code.count("}"):
            problems.append("unbalanced braces %d/%d" % (code.count("{"), code.count("}")))
        if not lines or lines[-1].strip() != "}":
            problems.append("truncated? last line %r" % (lines[-1] if lines else None))
        if _vc.leaked_catch_var(lines):
            problems.append("catch variable used outside its catch")
        if _vc.dead_after_return(lines):
            problems.append("dead code after a jump")
        real = [e for e in table if e[3] or not lr.is_monitor_cleanup(e[2])]
        if real and not re.search(r"\btry\b", text):
            problems.append("no try for %s" % real)
        if problems:
            totals["problems"] += 1
            print("%s %s: %s" % (rel.rsplit("/", 1)[-1], f.name, "; ".join(problems)))
            if SHOW:
                for l in lines:
                    print("   JAVA |", l)
        else:
            totals["ok"] += 1
    print("#### %s (%.1fs)" % (rel, time.time() - t))
    v.file.close()
print("TOTAL", totals)
