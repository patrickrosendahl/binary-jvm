"""Compare Pseudo Java with Vineflower (the decompiler Recaf uses) method by method.

Two inputs, both under OUT (default .scratch/vfcmp/, git-ignored):
  vf/<pkg/Class>.java   Vineflower output (this script runs it: --decompile)
  pj/<pkg/Class>.pj     Pseudo Java, dumped inside Binary Ninja by tests/bn_pseudo_java_dump.py

Per method it reports readability counters (stack temporaries, gotos, labels, synchronized comments,
`__offset` array stores, leaked exception plumbing, dead code after return, lines) and what Pseudo Java lost
against Vineflower: called method names and string literals that Vineflower has and Pseudo Java does not.

usage:
  python3 tests/vineflower_compare.py --decompile        # run Vineflower on CLASSES
  bnrun --parallel --timeout 600 tests/bn_pseudo_java_dump.py
  python3 tests/vineflower_compare.py [--show NAME] [--worst N] [--json FILE]
                                     [--max K=V ...]     # exit 1 if a total exceeds K (e.g. --max lost_calls=0)
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
    "temps": re.compile(r"\b(st\d+_lo(_\d+)?|st\d+_\d+|r_\d+|r64_\d+|r64|r)\b"),
    "gotos": re.compile(r"\bgoto\b"),
    "labels": re.compile(r"^\s*label_\w+:"),
    "sync_comments": re.compile(r"//\s*\}?\s*synchronized"),
    "offset_stores": re.compile(r"__offset"),
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
        code = strip_strings(re.sub(r"//.*", "", l))
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


def compare_method(pj_body, vf_body):
    pj_text = "\n".join(pj_body)
    r = {k: len(p.findall(pj_text)) if k != "labels" else sum(1 for l in pj_body if p.match(l))
         for k, p in COUNTERS.items()}
    r["temps"] = len(set(m.group(0) for m in COUNTERS["temps"].finditer(strip_strings(pj_text))))
    r["dead_code"] = dead_after_return(pj_body)
    r["leaked_catch_var"] = leaked_catch_var(pj_body)
    r["lines"] = sum(1 for l in pj_body if l.strip() and l.strip() not in "{}")
    if vf_body is not None:
        vf_text = "\n".join(vf_body)
        r["vf_lines"] = sum(1 for l in vf_body if l.strip() and l.strip() not in "{}")
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
    ap.add_argument("--json")
    ap.add_argument("--max", nargs="*", default=[], help="K=V: fail if total K exceeds V")
    a = ap.parse_args()
    classes = a.classes or DEFAULT_CLASSES
    if a.decompile:
        decompile(classes)
        return 0
    totals, rows = {}, []
    for rel in classes:
        simple = os.path.basename(rel)[:-len(".class")]
        pj_path = os.path.join(OUT, "pj", rel[:-len(".class")] + ".pj")
        vf_path = os.path.join(OUT, "vf", os.path.dirname(rel), simple + ".java")
        if not (os.path.exists(pj_path) and os.path.exists(vf_path)):
            print("missing", pj_path if not os.path.exists(pj_path) else vf_path)
            continue
        vf, _fields = vf_methods(vf_path, simple)
        pj = pj_methods(pj_path, simple)
        for key, body in pj.items():
            vbody = vf.get(key)
            if vbody is None and key.startswith(("<init>/", "<clinit>/")):
                vbody = []  # Vineflower dropped an empty / fully hoisted initialiser
            r = compare_method(body, vbody)
            r["method"] = "%s.%s" % (simple, key)
            rows.append(r)
            if a.show and a.show in r["method"]:
                print("=" * 30, r["method"])
                print("\n".join(body))
                print("-" * 30, "Vineflower")
                print("\n".join(vbody or ["(no match)"]))
    keys = ["lines", "vf_lines", "temps", "gotos", "labels", "sync_comments", "offset_stores", "plumbing", "dead_code",
            "leaked_catch_var"]
    for k in keys:
        totals[k] = sum(r.get(k, 0) for r in rows)
    totals["lost_calls"] = sum(len(r.get("lost_calls", [])) for r in rows)
    totals["lost_strings"] = sum(len(r.get("lost_strings", [])) for r in rows)
    totals["methods"] = len(rows)
    totals["unmatched"] = sum(1 for r in rows if "vf_lines" not in r)
    print("TOTAL", json.dumps(totals))
    bad = lambda r: (r["temps"] + 5 * (r["gotos"] + r["sync_comments"] + r["offset_stores"]) +
                     20 * (r["plumbing"] + r["dead_code"] + r["leaked_catch_var"]) +
                     10 * (len(r.get("lost_calls", [])) + len(r.get("lost_strings", []))))
    for r in sorted(rows, key=bad, reverse=True)[:a.worst]:
        if bad(r) == 0:
            break
        extra = {k: r[k] for k in keys[2:] if r.get(k)}
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
