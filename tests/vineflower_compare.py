"""Compare Pseudo Java with Vineflower (the decompiler Recaf uses) method by method.

Two inputs, both under OUT (default .scratch/vfcmp/, git-ignored):
  vf/<pkg/Class>.java   Vineflower output (this script runs it: --decompile)
  pj/<pkg/Class>.pj     Pseudo Java, dumped inside Binary Ninja by tests/bn_pseudo_java_dump.py

Per method it reports readability counters (stack temporaries, gotos, labels, synchronized comments,
`__offset` array stores, `while (true)` / `do` loops, leaked exception plumbing, dead code after return, lines) and what Pseudo Java lost
against Vineflower: called method names and string literals that Vineflower has and Pseudo Java does not.
Size is measured in statements (stmts / vf_stmts: comments dropped, wrapped lines joined, as both sides wrap
differently); EXCESS splits the extra statements by a guessed cause (--excess N lists the worst methods).

usage:
  python3 tests/vineflower_compare.py --decompile        # run Vineflower on CLASSES
  bnrun --parallel --timeout 600 tests/bn_pseudo_java_dump.py
  python3 tests/vineflower_compare.py [--show NAME] [--worst N] [--excess N] [--json FILE]
                                     [--pj DIR] [--max K=V ...]  # exit 1 if a total exceeds V (e.g. --max lost_calls=0)
"""
import argparse, glob, json, os, re, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO, "sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib")
CLASS_DIR = os.path.join(LIB, "mdg")
OUT = os.path.join(REPO, ".scratch", "vfcmp")
VINEFLOWER = os.path.expanduser("~/dev/vineflower/build/libs/vineflower-1.12.0-recaf.2.jar")
DEFAULT_CLASSES = [
    "com/is_teledata/cache/HashCache.class",
    "com/is_teledata/cache/SortedCache.class",
    "com/is_teledata/util/Scheduler.class",
    "com/is_teledata/util/Request.class",
    "com/is_teledata/mdg/RequestCollector.class",
    "com/is_teledata/mdg/MDGAttributeDefinition.class",
    "com/is_teledata/mdg/FormatDefinition.class",
    "com/is_teledata/mdg/MDGStats.class",
    "com/is_teledata/mdg/TimeZoneObject.class",
    "com/is_teledata/mdg/XidProducer.class",
    "com/is_teledata/mdg/push/Subscription.class",
    "com/is_teledata/mdg/server/HtmlConverter.class",
    "com/is_teledata/mdg/applet/AppletValueGetter.class",
    "com/is_teledata/log/Setup.class",
    "com/is_teledata/log/Logger.class",
    "com/is_teledata/log/LogProperties.class",
    "com/is_teledata/stats/StatsHandler.class",
    "com/is_teledata/property/PropLoader.class",
    "com/is_teledata/property/ValueContainer.class",
    "com/is_teledata/net/NetProperties.class",
]

# Calls Vineflower writes that Pseudo Java legitimately may not (or the other way round): javac desugaring
# that one side folds and the other keeps.
CALL_IGNORE = {"append", "toString", "valueOf", "intValue", "longValue", "booleanValue", "doubleValue",
               "floatValue", "shortValue", "byteValue", "charValue", "makeConcatWithConstants", "super", "this",
               "if", "while", "for", "switch", "catch", "synchronized", "return", "new", "throw"}
COUNTERS = {
    # stack temporaries, and copies of a static field that BN named Class_field_N (jvm-60)
    "temps": re.compile(r"\b(st\d+_lo(_\d+)?|st\d+_\d+|r_\d+|r64_\d+|r64|r|[A-Z][A-Za-z0-9$]*_[\w$]+_\d+)\b"),
    "gotos": re.compile(r"\bgoto\b"),
    "labels": re.compile(r"^\s*label_\w+:"),
    "sync_comments": re.compile(r"//\s*\}?\s*synchronized"),
    "offset_stores": re.compile(r"__offset"),
    "while_true": re.compile(r"\bwhile \(true\)|\bdo \{"),
    "plumbing": re.compile(r"\b(exc(_\d+)?|__exception|__propagate)\b|\binstanceof\(|could not render|render raised"),
}


def method_key(name, nparams):
    return "%s/%d" % (name, nparams)


def count_params(params):
    params = params.strip()
    if not params:
        return 0
    depth, n = 0, 1
    for ch in params:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            n += 1
    return n


VF_HEADER = re.compile(r"^   (?:@\w+\s*)*(?P<mods>(?:(?:public|private|protected|static|final|synchronized|native|abstract|strictfp)\s+)*)"
                       r"(?:<[^>]*>\s*)?(?:(?P<ret>[\w.$\[\]<>?, ]+?)\s+)?(?P<name>[\w$]+)\((?P<params>[^)]*)\)[^;{]*\{\s*$")
VF_STATIC = re.compile(r"^   static \{\s*$")


def vf_methods(path, simple):
    """Top-level methods of a Vineflower .java file: {key: body lines}. Constructors become <init>, static
    initialisers <clinit>; field initialisers are reported separately (Vineflower hoists them)."""
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    out, fields, i = {}, [], 0
    while i < len(lines):
        l = lines[i]
        m = VF_HEADER.match(l)
        st = VF_STATIC.match(l)
        if m or st:
            if st:
                key = method_key("<clinit>", 0)
            else:
                name = m.group("name")
                if name == simple and not m.group("ret"):
                    name = "<init>"
                key = method_key(name, count_params(m.group("params")))
            j = i + 1
            while j < len(lines) and lines[j] != "   }":
                j += 1
            out[key] = lines[i:j + 1]
            i = j + 1
            continue
        if re.match(r"^   [\w<].*=.*;\s*$", l):
            fields.append(l.strip())
        i += 1
    return out, fields


PJ_HEADER = re.compile(r"^(?P<pre>[^(]*?)(?P<name>[\w$<>]+)\((?P<params>[^)]*)\)\s*$")


def pj_methods(path, simple):
    out, cur, name = {}, None, None
    for l in open(path, encoding="utf-8", errors="replace").read().splitlines():
        if l.startswith("==== "):
            name = re.sub(r"\(\d+\)$", "", l[5:].strip())
            cur = []
            out.setdefault(name, []).append(cur)
            continue
        if cur is not None:
            cur.append(l)
    keyed = {}
    for name, bodies in out.items():
        for body in bodies:
            n = 0
            if body and body[0].strip() != "static":
                m = PJ_HEADER.match(body[0].strip())
                if m:
                    n = count_params(m.group("params"))
            keyed[method_key(name, n)] = body
    return keyed


def strip_strings(text):
    return re.sub(r'"(\\.|[^"\\])*"', '""', text)


def string_literals(text):
    flat = re.sub(r'"\s*\n\s*\+?\s*"', "", text)
    return {m.group(0) for m in re.finditer(r'"(\\.|[^"\\])*"', flat)}


def called_names(text):
    code = strip_strings(re.sub(r"//[^\n]*", "", text))
    names = set(re.findall(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\(", code))
    return {n for n in names if n not in CALL_IGNORE and not n[0].isupper()}


def dead_after_return(lines):
    n = 0
    for a, b in zip(lines, lines[1:]):
        if re.match(r"^\s*(return\b.*|throw\b.*|break|continue);\s*$", a.strip() and a or "x") and \
                re.match(r"^\s*(return|break|continue|throw)\b", b) and \
                len(a) - len(a.lstrip()) == len(b) - len(b.lstrip()):
            n += 1
    return n


def leaked_catch_var(lines):
    """A catch variable used outside its catch block (e.g. `if (e != 0 ...)` in the try body)."""
    n, depth, stack = 0, 0, []
    names = sorted({m.group(1) for l in lines
                    for m in [re.search(r"catch \((?:[\w.$]+\s*\|\s*)*[\w.$]+ (\w+)\)", l)] if m})
    for l in lines:
        code = re.sub(r"\.\s*[\w$]+", ".", strip_strings(re.sub(r"//.*", "", l)))  # member names are not variables
        m = re.search(r"catch \((?:[\w.$]+\s*\|\s*)*[\w.$]+ (\w+)\)", code)
        if m:
            stack.append((m.group(1), depth - code[:m.start()].count("}")))
        for v in names:
            declared = re.search(r"^\s*[\w.$<>\[\]]+ %s\b" % re.escape(v), code)
            if re.search(r"\b%s\b" % re.escape(v), code) and not declared and not any(s[0] == v for s in stack):
                n += 1
                break
        depth += code.count("{") - code.count("}")
        while stack and depth <= stack[-1][1]:
            stack.pop()
    return n


PRIMITIVE_TYPES = {"int", "long", "short", "byte", "char", "boolean", "float", "double"}
JTYPE = r"[A-Za-z_$][\w$.]*(?:<[^;=()]*?>)?(?:\[\])*"
DECL = re.compile(r"(?:^|[(,;]\s*|\bfor \()\s*(?:final\s+)?(%s)\s+([A-Za-z_$][\w$]*)\s*(?=[=;,)])" % JTYPE)
NOT_TYPES = {"return", "throw", "new", "else", "case", "goto", "break", "continue", "instanceof", "this", "super"}
# assignments from a value of a known type that Java would reject without a cast
STRING_SUPERS = {"String", "Object", "CharSequence", "Comparable", "Serializable"}
BOXED = {"Integer": "int", "Long": "long", "Short": "short", "Byte": "byte", "Character": "char", "Boolean": "boolean",
         "Float": "float", "Double": "double"}


def declared_types(lines):
    """name -> declared Java type for parameters (header line), locals and catch variables of one method"""
    out = {}
    for l in lines:
        code = strip_strings(re.sub(r"//.*", "", l))
        for m in DECL.finditer(code):
            if m.group(1) not in NOT_TYPES:
                out.setdefault(m.group(2), m.group(1))
    return out


def value_type(expr, types):
    """static type of a simple expression from the declarations: a variable, an array element `a[i]`, a cast,
    a string literal; None if unknown"""
    expr = expr.strip()
    if re.fullmatch(r'"(\\.|[^"\\])*"', expr):
        return "String"
    m = re.fullmatch(r"\((%s)\)\s*[\w$.\[\]]+" % JTYPE, expr)
    if m and m.group(1) not in PRIMITIVE_TYPES | {"this"}:
        return m.group(1)
    m = re.fullmatch(r"([A-Za-z_$][\w$]*)((?:\[[^\[\]]+\])*)", expr)
    if m and m.group(1) in types:
        t, dims = types[m.group(1)], m.group(2).count("[")
        if t.count("[]") < dims:
            return None
        return t[:len(t) - 2 * dims] if dims else t
    return None


def type_problems(lines):
    """(reference compared with 0, double casts, declared-type mismatches) of one method's Pseudo Java"""
    types = declared_types(lines)
    ref_zero = casts = mismatch = 0
    for l in lines:
        code = strip_strings(re.sub(r"//.*", "", l))
        for m in re.finditer(r"([A-Za-z_$][\w$]*(?:\[[^\[\]]+\])*)\s*[!=]=\s*0(?![\w.])", code):
            t = value_type(m.group(1), types)
            if t is not None and t not in PRIMITIVE_TYPES:
                ref_zero += 1
        casts += len(re.findall(r"(?<![\w$)\]])\((%s)\)\s*\(\1\)" % JTYPE, code))
        for m in re.finditer(r"(?:^|[{;])\s*(?:(%s)\s+)?([A-Za-z_$][\w$]*)\s*=\s*([^=;][^;]*);" % JTYPE, code):
            decl, name, rhs = m.group(1), m.group(2), m.group(3)
            if decl in NOT_TYPES:
                continue
            target = decl or types.get(name)
            src = value_type(rhs, types)
            if target is None or src is None or target == src:
                continue
            if src == "String" and target not in STRING_SUPERS:
                mismatch += 1
            elif src not in PRIMITIVE_TYPES and target not in PRIMITIVE_TYPES and src == "Object" and \
                    target != "Object":
                mismatch += 1  # a downcast without a cast
            elif (src in PRIMITIVE_TYPES) != (target in PRIMITIVE_TYPES) and src != "Object" and target != "Object" \
                    and BOXED.get(src, src) != BOXED.get(target, target):
                mismatch += 1  # (boxing is hidden: `int n = integer` is fine)
    return ref_zero, casts, mismatch


IF_ELSE_ASSIGN = re.compile(r"^\s*(%s) ([\w$]+);\n\s*if \(.*\) \{\n\s*\2 = [^\n]*;\n\s*\} else \{\n\s*\2 = [^\n]*;\n"
                            r"\s*\}$" % JTYPE, re.M)


def if_else_assigns(lines):
    """`T x; if (c) { x = a; } else { x = b; }` -- a conditional expression in Vineflower (not when a value holds
    one already: Vineflower does not nest them either)"""
    return sum(1 for m in IF_ELSE_ASSIGN.finditer("\n".join(l for l in lines if l.strip()))
               if " ? " not in strip_strings(m.group(0)))


def statements(lines):
    """logical lines: comments dropped, wrapped continuation lines joined (a line that does not end with
    `;`, `{`, `}` or `:` continues on the next one), lines of only braces not counted"""
    out, cur = [], ""
    for l in lines:
        code = re.sub(r'("(?:\\.|[^"\\])*")|//.*', lambda m: m.group(1) or "", l).strip()
        if not code:
            continue
        cur = cur + " " + code if cur else code
        if code[-1] in ";{}:":
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return [s for s in out if s.replace(" ", "") not in ("", "{", "}", "{}")]


INC_COPY = re.compile(r"^(?:%s) ([\w$]+) = ([\w$.]+)( [+-] 1)?;\n\2 = (?:\1 [+-] 1|\1);" % JTYPE, re.M)
FIELD_COPY = re.compile(r"^(?:%s) [A-Z][\w$]*_[\w$]+_\d+ = [\w$.]+;" % JTYPE, re.M)
BARE_DECL = re.compile(r"^(?:final )?(%s) [\w$]+;$" % JTYPE)
RETURN = re.compile(r"^(?:\} else \{ )?return\b")
BOOL_RETURN = re.compile(r"^if \(.*\{\n(?:\} else \{\n)?return (true|false);\n(?:\} else \{\n)?return (?!\1)(true|false);$",
                         re.M)
ARRAY_STORE = re.compile(r"^(?:%s )?([\w$]+) = new [\w$.]+\[\d+\];\n((?:\1\[\d+\] = [^\n]*;\n?)+)" % JTYPE, re.M)


def excess_causes(pj_body, pj_stmts, vf_stmts, hoisted=()):
    """a guess where Pseudo Java's extra statements come from: {cause: statements}; 'other' is the rest.
    hoisted: fields Vineflower initialises in their declaration (only for <init> / <clinit>)"""
    pj_text = "\n".join(pj_stmts)
    excess = len(pj_stmts) - len(vf_stmts)
    causes = {}
    if excess <= 0:
        return causes
    causes["hoisted_field"] = sum(1 for s in pj_stmts
                                  for m in [re.match(r"^(?:this|[A-Z][\w$]*)\.([\w$]+) = ", s)] if m and m.group(1) in hoisted)
    if hoisted and not vf_stmts:
        causes["hoisted_field"] += 1  # the header of an initialiser Vineflower dropped
    ternary = if_else_assigns(pj_body)
    causes["ternary"] = 4 * ternary  # T x; / if (c) { / x = a; / } else { / x = b; -- one statement in Vineflower
    causes["bool_return"] = 2 * len(BOOL_RETURN.findall(pj_text))  # if (c) { return false; } return true;
    causes["array_init"] = sum(m.group(2).count("\n") + 1 - m.group(2).endswith("\n")
                               for m in ARRAY_STORE.finditer(pj_text))  # new T[n] + element stores
    causes["increment"] = 2 * len(INC_COPY.findall(pj_text))
    causes["field_copy"] = len(FIELD_COPY.findall(INC_COPY.sub("", pj_text)))
    count = lambda p, ss: sum(1 for s in ss if p.match(s) and s.split()[0] not in NOT_TYPES)
    causes["split_decl"] = max(0, count(BARE_DECL, pj_stmts) - count(BARE_DECL, vf_stmts) - ternary)
    causes["early_return"] = max(0, sum(1 for s in pj_stmts if RETURN.match(s)) -
                                 sum(1 for s in vf_stmts if RETURN.match(s)) - causes["bool_return"] // 2)
    left = excess
    for k in list(causes):
        causes[k] = min(causes[k], left)
        left -= causes[k]
    causes["other"] = left
    return {k: v for k, v in causes.items() if v}


def compare_method(pj_body, vf_body, hoisted=()):
    pj_text = "\n".join(pj_body)
    r = {k: len(p.findall(pj_text)) if k != "labels" else sum(1 for l in pj_body if p.match(l))
         for k, p in COUNTERS.items()}
    r["temps"] = len(set(m.group(0) for m in COUNTERS["temps"].finditer(strip_strings(pj_text))))
    r["dead_code"] = dead_after_return(pj_body)
    r["leaked_catch_var"] = leaked_catch_var(pj_body)
    r["ref_zero"], r["double_casts"], r["type_mismatch"] = type_problems(pj_body)
    r["if_else_assign"] = if_else_assigns(pj_body)
    r["lines"] = sum(1 for l in pj_body if l.strip() and l.strip() not in "{}")
    pj_stmts = statements(pj_body)
    r["stmts"] = len(pj_stmts)
    if vf_body is not None:
        vf_text = "\n".join(vf_body)
        r["vf_lines"] = sum(1 for l in vf_body if l.strip() and l.strip() not in "{}")
        vf_stmts = statements(vf_body)
        r["vf_stmts"] = len(vf_stmts)
        r["causes"] = excess_causes(pj_body, pj_stmts, vf_stmts, hoisted)
        lost = called_names(vf_text) - called_names(pj_text)
        r["lost_calls"] = sorted(lost)
        pj_strings = {s.replace(" ", "") for s in string_literals(pj_text)}
        r["lost_strings"] = sorted(s for s in string_literals(vf_text) if s.replace(" ", "") not in pj_strings)
    return r


def decompile(classes):
    vf_dir = os.path.join(OUT, "vf")
    for rel in classes:
        base = os.path.join(CLASS_DIR, rel[:-len(".class")])
        files = [base + ".class"] + sorted(glob.glob(glob.escape(base) + "$*.class"))
        dst = os.path.join(vf_dir, os.path.dirname(rel))
        os.makedirs(dst, exist_ok=True)
        subprocess.run(["java", "-jar", VINEFLOWER, "-log=WARN", "-e=" + os.path.join(LIB, "mdg.jar")] + files + [dst],
                       check=True, stdout=subprocess.DEVNULL)
    print("Vineflower output in", vf_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decompile", action="store_true")
    ap.add_argument("--classes", nargs="*", default=None)
    ap.add_argument("--show", help="print both versions of methods whose name contains this")
    ap.add_argument("--worst", type=int, default=15)
    ap.add_argument("--excess", type=int, default=0, help="list the N methods with the most extra statements")
    ap.add_argument("--json")
    ap.add_argument("--pj", default=os.path.join(OUT, "pj"), help="directory of the Pseudo Java dump")
    ap.add_argument("--max", nargs="*", default=[], help="K=V: fail if total K exceeds V")
    a = ap.parse_args()
    classes = a.classes or DEFAULT_CLASSES
    if a.decompile:
        decompile(classes)
        return 0
    totals, rows = {}, []
    for rel in classes:
        simple = os.path.basename(rel)[:-len(".class")]
        pj_path = os.path.join(a.pj, rel[:-len(".class")] + ".pj")
        vf_path = os.path.join(OUT, "vf", os.path.dirname(rel), simple + ".java")
        if not (os.path.exists(pj_path) and os.path.exists(vf_path)):
            print("missing", pj_path if not os.path.exists(pj_path) else vf_path)
            continue
        vf, fields = vf_methods(vf_path, simple)
        hoisted = {m.group(1) for f in fields for m in [re.search(r"([\w$]+)\s*=", f)] if m}
        pj = pj_methods(pj_path, simple)
        for key, body in pj.items():
            vbody = vf.get(key)
            if vbody is None and key.startswith(("<init>/", "<clinit>/")):
                vbody = []  # Vineflower dropped an empty / fully hoisted initialiser
            r = compare_method(body, vbody, hoisted if key.startswith(("<init>/", "<clinit>/")) else ())
            r["method"] = "%s.%s" % (simple, key)
            rows.append(r)
            if a.show and a.show in r["method"]:
                print("=" * 30, r["method"])
                print("\n".join(body))
                print("-" * 30, "Vineflower")
                print("\n".join(vbody or ["(no match)"]))
    keys = ["stmts", "vf_stmts", "lines", "vf_lines", "temps", "gotos", "labels", "sync_comments", "offset_stores", "while_true", "plumbing",
            "dead_code", "leaked_catch_var", "ref_zero", "double_casts", "type_mismatch", "if_else_assign"]
    for k in keys:
        totals[k] = sum(r.get(k, 0) for r in rows)
    totals["lost_calls"] = sum(len(r.get("lost_calls", [])) for r in rows)
    totals["lost_strings"] = sum(len(r.get("lost_strings", [])) for r in rows)
    totals["methods"] = len(rows)
    totals["unmatched"] = sum(1 for r in rows if "vf_lines" not in r)
    matched = [r for r in rows if "vf_stmts" in r]
    totals["more_stmts"] = sum(1 for r in matched if r["stmts"] > r["vf_stmts"])
    totals["fewer_stmts"] = sum(1 for r in matched if r["stmts"] < r["vf_stmts"])
    causes = {}
    for r in matched:
        for k, v in r["causes"].items():
            causes[k] = causes.get(k, 0) + v
    print("TOTAL", json.dumps(totals))
    print("EXCESS", json.dumps(dict(sorted(causes.items(), key=lambda kv: -kv[1]))))
    for r in sorted(matched, key=lambda r: r["vf_stmts"] - r["stmts"])[:a.excess]:
        if r["stmts"] <= r["vf_stmts"]:
            break
        print("  %-45s %3d/%-3d statements %s" % (r["method"], r["stmts"], r["vf_stmts"], r["causes"]))
    bad = lambda r: (r["temps"] + 5 * (r["gotos"] + r["sync_comments"] + r["offset_stores"]) +
                     20 * (r["plumbing"] + r["dead_code"] + r["leaked_catch_var"]) +
                     10 * (r["ref_zero"] + r["double_casts"] + r["type_mismatch"]) +
                     10 * (len(r.get("lost_calls", [])) + len(r.get("lost_strings", []))))
    for r in sorted(rows, key=bad, reverse=True)[:a.worst]:
        if bad(r) == 0:
            break
        extra = {k: r[k] for k in keys[4:] if r.get(k)}
        if r.get("lost_calls"):
            extra["lost_calls"] = r["lost_calls"]
        if r.get("lost_strings"):
            extra["lost_strings"] = [s[:30] for s in r["lost_strings"]]
        print("  %-45s %3d/%-3s lines %s" % (r["method"], r["lines"], r.get("vf_lines", "?"), extra))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"totals": totals, "methods": rows}, fh, indent=1)
    failed = []
    for kv in a.max:
        k, v = kv.split("=")
        if totals.get(k, 0) > int(v):
            failed.append("%s=%d > %s" % (k, totals[k], v))
    if failed:
        print("FAIL", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
