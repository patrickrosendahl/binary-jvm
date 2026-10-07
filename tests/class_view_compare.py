"""Compare the declarations of the class view (`JVM\\Show class`, jvm-41/62) with Vineflower's: imports, the
class header, fields and method headers (modifiers, generic types, @Override, throws; parameter names are
ignored, bodies and initialisers too). Inputs under .scratch/vfcmp/: vf/ (tests/vineflower_compare.py
--decompile) and cv/ (tests/bn_class_view_dump.py).

usage:
  python3 tests/class_view_compare.py [--classes C ...] [--show] [--max K=V ...]   # exit 1 if a total exceeds V
"""
import argparse, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vineflower_compare as vc

OUT = vc.OUT


def split_params(params):
    out, depth, cur = [], 0, ""
    for ch in params:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [re.sub(r"^final ", "", p.strip()).rsplit(" ", 1)[0] for p in out]  # (captured parameters are final)


MEMBER_METHOD = re.compile(r"^(?P<head>[^=(]*?)(?P<name>[\w$<>]+)\((?P<params>[^)]*)\)(?:\s*throws\s+(?P<throws>[^{;]+))?")


def declarations(lines, indent):
    """{'imports': set, 'header': str, 'fields': set, 'methods': {key: decl}} of the top-level class; members
    are the lines at exactly `indent` spaces"""
    out = {"imports": set(), "header": "", "fields": set(), "methods": {}}
    override = False
    for l in lines:
        s = l.strip()
        if s.startswith("import "):
            out["imports"].add(s)
            continue
        if not out["header"] and re.match(r"^(public |final |abstract )*(class|interface|enum) ", l):
            out["header"] = s.rstrip("{ ").strip()
            continue
        if not out["header"] or len(l) - len(l.lstrip()) != indent or not s or s.startswith("//"):
            continue
        if s == "@Override":
            override = True
            continue
        s = re.sub(r"\s*//.*$", "", s)
        if s.startswith("static {") or s == "static":
            override = False
            continue
        m = MEMBER_METHOD.match(s)
        if m and "=" not in s.split("(")[0]:
            words = m.group("head").split()
            throws = sorted(t.strip() for t in (m.group("throws") or "").split(",") if t.strip())
            key = "%s(%s)" % (m.group("name"), ", ".join(split_params(m.group("params"))))
            out["methods"][key] = {"override": override, "head": " ".join(words), "throws": throws}
        elif s.endswith(";") or "=" in s:
            out["fields"].add(re.sub(r"\s*=.*$", "", s).rstrip(";").strip())
        override = False
    return out


def compare(cv, vf, simple):
    r = {"missing_imports": sorted(vf["imports"] - cv["imports"]), "extra_imports": sorted(cv["imports"] - vf["imports"]),
         "header": None if cv["header"] == vf["header"] else (cv["header"], vf["header"]),
         "fields": sorted(vf["fields"] ^ cv["fields"])}
    methods = []
    for key, v in vf["methods"].items():
        k = key.replace(simple + "(", "<init>(", 1) if key.startswith(simple + "(") else key
        c = cv["methods"].get(k) or cv["methods"].get(key)
        if c is None:
            methods.append(("missing", key))
            continue
        for what in ("override", "head", "throws"):
            if c[what] != v[what]:
                methods.append((what, key, c[what], v[what]))
    r["methods"] = methods
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classes", nargs="*", default=None)
    ap.add_argument("--cv", default=os.path.join(OUT, "cv"))
    ap.add_argument("--show", action="store_true", help="print every difference")
    ap.add_argument("--max", nargs="*", default=[])
    a = ap.parse_args()
    totals = {"classes": 0, "imports": 0, "headers": 0, "fields": 0, "override": 0, "head": 0, "throws": 0,
              "missing": 0}
    for rel in a.classes or vc.DEFAULT_CLASSES:
        simple = os.path.basename(rel)[:-len(".class")]
        cv_path = os.path.join(a.cv, rel[:-len(".class")] + ".java")
        vf_path = os.path.join(OUT, "vf", os.path.dirname(rel), simple + ".java")
        if not (os.path.exists(cv_path) and os.path.exists(vf_path)):
            print("missing", cv_path if not os.path.exists(cv_path) else vf_path)
            continue
        cv = declarations(open(cv_path).read().splitlines(), 4)
        vf = declarations(open(vf_path).read().splitlines(), 3)
        r = compare(cv, vf, simple)
        totals["classes"] += 1
        totals["imports"] += len(r["missing_imports"]) + len(r["extra_imports"])
        totals["headers"] += r["header"] is not None
        totals["fields"] += len(r["fields"])
        for m in r["methods"]:
            totals[m[0]] += 1
        if a.show and (r["missing_imports"] or r["extra_imports"] or r["header"] or r["fields"] or r["methods"]):
            print("==", simple)
            for k in ("missing_imports", "extra_imports", "header", "fields"):
                if r[k]:
                    print("  %s: %s" % (k, r[k]))
            for m in r["methods"]:
                print("  method", m)
    print("TOTAL", json.dumps(totals))
    failed = ["%s=%d > %s" % (k, totals.get(k, 0), v) for k, v in (kv.split("=") for kv in a.max)
              if totals.get(k, 0) > int(v)]
    if failed:
        print("FAIL", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
