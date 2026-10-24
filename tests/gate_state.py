"""Bookkeeping of the gate runs: output stamps (jvm-105), RESUME (jvm-106) and saved reference totals (jvm-107).

Stamps (after binary-cpp's tests/suite/suite.py, bncpp-134/141): every dump writes <dump dir>/.stamps.json,
{rel: stamp} per input class, where a stamp holds the hashes of what produced the output -- 'plugin' the loaded
package (the top-level *.py that bn_dev_load.py copies; the names a dev load renames are left out), 'scripts' the
dump scripts read from the repo, 'input' the class file (+ its Outer$*.class siblings, which Pseudo Java reads for
anonymous classes and lambdas) -- plus 'dev' (the package name), 'path' (the input) and 'at' (the time).
The compare scripts warn when the outputs carry no stamp, come from several plugin states or not from the working
tree's plugin / scripts / class files (stale: e.g. a gate run on an old jvm_devN); --strict exits 1 on a warning.
RESUME=1 in the dump scripts skips a class whose output carries the current stamp.

Reference totals (after binary-cpp's check --compare, bncpp-131): --save-ref NAME stores a compare run's TOTAL as
<dump dir>/../refs/NAME.<kind>.json (kind: pj / cv / ...), --ref NAME prints that run's totals (REF) and the
differences (DELTA) under TOTAL -- the before / after numbers of a ticket. --ref also takes a JSON file path
(a --save-ref file or a --json output).

Pure Python, no Binary Ninja import: used inside BN by the dump scripts and outside by the compare scripts.
Language-neutral: a shared/ candidate (jvm-68).
"""
import fcntl, hashlib, json, os, re, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMPS = ".stamps.json"
# what each dump kind runs; their hashes make the 'scripts' part of a stamp
SCRIPTS = {"pj": ["bn_pseudo_java_dump.py"], "cv": ["bn_class_view_dump.py"],
           "hlil": ["bn_pseudo_java_compare.py", "vineflower_compare.py"]}
# names bn_dev_load.py renames in its copy (constants.py, view.py)
_DEV_NAMES = re.compile(rb'^([ \t]*(?:ARCH_NAME|VIEW_NAME|long_name) = )"[^"]*"', re.M)


def _sha(parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(p if isinstance(p, bytes) else p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:12]


def plugin_hash(root=REPO):
    """the plugin sources under root (the repo, or a dev load's /tmp/jvm_devN), as bn_dev_load.py copies them"""
    parts = []
    for f in sorted(os.listdir(root)):
        if f.endswith(".py"):
            parts += [f, _DEV_NAMES.sub(rb"\1", open(os.path.join(root, f), "rb").read())]
    return _sha(parts)


def scripts_hash(kind):
    return _sha([open(os.path.join(REPO, "tests", f), "rb").read() for f in SCRIPTS[kind]])


def input_hash(path):
    d, base = os.path.split(path)
    stem = base[:-len(".class")]
    sibs = sorted(f for f in os.listdir(d) if f.startswith(stem + "$") and f.endswith(".class")) \
        if os.path.isdir(d) else []
    return _sha([open(os.path.join(d, f), "rb").read() for f in [base] + sibs])


def read_stamps(dump_dir):
    try:
        with open(os.path.join(dump_dir, STAMPS), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_stamp(dump_dir, rel, stamp):
    """merge the stamp of one class into dump_dir/.stamps.json; the parallel batches write the same file, so
    under an flock (per open file on macOS, so it also holds between threads of the BN process)"""
    os.makedirs(dump_dir, exist_ok=True)
    with open(os.path.join(dump_dir, STAMPS + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        allst = read_stamps(dump_dir)
        allst[rel] = dict(stamp, at=time.strftime("%Y-%m-%d %H:%M:%S"))
        tmp = os.path.join(dump_dir, STAMPS + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(allst, fh, indent=1, sort_keys=True)
        os.replace(tmp, os.path.join(dump_dir, STAMPS))


class Stamper:
    """used by a dump script inside BN: Stamper(kind, package dir, package name, dump dir); extra: settings that
    change the output (part of the stamp)"""

    def __init__(self, kind, pkg_dir, dev, dump_dir, extra=None):
        self.base = dict(extra or {}, plugin=plugin_hash(pkg_dir), scripts=scripts_hash(kind), dev=dev)
        self.dump_dir = dump_dir
        self.have = read_stamps(dump_dir)

    def want(self, path):
        return dict(self.base, input=input_hash(path), path=path)

    def current(self, rel, path, outputs):
        """RESUME: the outputs exist and carry the stamp this run would write"""
        st = self.have.get(rel)
        return bool(st) and all(os.path.exists(o) for o in outputs) and \
            all(st.get(k) == v for k, v in self.want(path).items() if k != "dev")

    def stamp(self, rel, path):
        write_stamp(self.dump_dir, rel, self.want(path))


def stamp_warnings(dump_dir, kind, classes):
    """warnings about the outputs of classes in dump_dir: unstamped, from several plugin states, not from the
    working tree's plugin sources / dump scripts / class files"""
    stamps = read_stamps(dump_dir)
    plugin, scripts = plugin_hash(), scripts_hash(kind)
    devs, unstamped, stale = {}, 0, {"plugin": 0, "scripts": 0, "input": 0}
    for rel in classes:
        st = stamps.get(rel)
        if st is None:
            unstamped += 1
            continue
        devs.setdefault(st["plugin"], set()).add(st.get("dev", "?"))
        path = st.get("path")
        now_input = input_hash(path) if path and os.path.exists(path) else None
        for key, now in (("plugin", plugin), ("scripts", scripts), ("input", now_input)):
            stale[key] += st.get(key) != now
    out = []
    if unstamped:
        out.append("%d output(s) in %s without a stamp (dumped before jvm-105, or not dumped)" % (unstamped, dump_dir))
    if len(devs) > 1:
        out.append("outputs from %d plugin states: %s" % (len(devs), "; ".join(
            "%s %s" % (h, "/".join(sorted(d))) for h, d in sorted(devs.items()))))
    what = {"plugin": "the working tree's plugin sources (edited since the dev load, or an older dump)",
            "scripts": "the current dump scripts", "input": "the current class files"}
    out += ["%d output(s) not from %s" % (n, what[k]) for k, n in stale.items() if n]
    return out


def warn_stamps(dump_dir, kind, classes):
    """print the stamp warnings; their count"""
    warnings = stamp_warnings(dump_dir, kind, classes)
    for w in warnings:
        print("WARNING", w)
    return len(warnings)


def _ref_path(dump_dir, name):
    if name.endswith(".json") or os.sep in name:
        return name
    dump_dir = os.path.abspath(dump_dir)
    return os.path.join(os.path.dirname(dump_dir), "refs", "%s.%s.json" % (name, os.path.basename(dump_dir)))


def save_ref(dump_dir, name, totals):
    path = _ref_path(dump_dir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamps = read_stamps(dump_dir)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"totals": totals, "plugins": sorted({s["plugin"] for s in stamps.values()}),
                   "at": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=1)
    print("saved reference totals to", path)


def print_ref(dump_dir, name, totals):
    """REF and DELTA lines under TOTAL"""
    path = _ref_path(dump_dir, name)
    if not os.path.exists(path):
        print("REF   none: no %s (save one with --save-ref %s)" % (path, name))
        return
    with open(path, encoding="utf-8") as fh:
        ref = json.load(fh)
    before = ref.get("totals", ref)
    print("REF  ", json.dumps(before), "(%s%s)" % (path, ", " + ref["at"] if "at" in ref else ""))
    delta = {k: totals.get(k, 0) - before.get(k, 0) for k in list(totals) + [k for k in before if k not in totals]
             if isinstance(totals.get(k, 0), int) and isinstance(before.get(k, 0), int)
             and totals.get(k, 0) != before.get(k, 0)}
    print("DELTA", " ".join("%s=%+d" % kv for kv in delta.items()) or "none")
