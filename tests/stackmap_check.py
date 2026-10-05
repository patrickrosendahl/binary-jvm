"""Offline check of stackmap.compute_stack_states (no Binary Ninja licence needed).

Parses every .class in the given .class/.jar files and computes the operand-stack shape of every
method. Reports merge conflicts / stack errors (verified bytecode has none), methods whose computed
maximum depth differs from the class file's max_stack (greater is an error, smaller is noted),
and unreachable instructions. Runs in parallel over chunks of classes.

usage: python3 tests/stackmap_check.py [--all | file.jar|file.class ...]
       no arguments: sample/.../lib/mdg.jar;  --all: every .jar/.class under sample/
"""
import collections, multiprocessing, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import offline_lift_check as olc   # binaryninja stub + plugin package loader + jar chunking
from binary_jvm import classfile, opcodes, stackmap

KEYS = ("errors", "depth_over")

def sample_dir():
    sample = os.path.join(olc.ROOT, "sample")
    if os.path.isdir(sample):
        return sample
    # in a git worktree the (git-ignored) samples live in the main checkout
    common = subprocess.run(["git", "-C", olc.ROOT, "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    return os.path.join(os.path.dirname(os.path.abspath(os.path.join(olc.ROOT, common))), "sample")

def instruction_offsets(code, base):
    off, offsets = 0, []
    while off < len(code):
        name, _, length, _ = opcodes.decode_instruction(code[off:], base + off)
        if name is None:
            break
        offsets.append(off)
        off += length
    return offsets

def check_class(label, raw, stats):
    reader = classfile.JVMClassReader(olc.Dummy(), olc.Data(raw))
    reader.charType = olc.Dummy()
    cls = classfile.JVMClassStructure(reader)
    for m in cls.methods:
        if m.code_attribute is None:
            continue
        attr = m.code_attribute.attribute
        base = 0x1000000 + 0x100000*m.index
        code = raw[attr.start_address:attr.end_address]
        states, depth, errors = stackmap.compute_stack_states(code, base, attr.exception_table, reader.memberDescriptor)
        stats["methods"] += 1
        stats["instructions"] += len(states)
        where = "%s m%d" % (label, m.index)
        stats["errors"] += ["%s %s" % (where, e) for e in errors]
        if depth > attr.max_stack:
            stats["depth_over"].append("%s: depth %d > max_stack %d" % (where, depth, attr.max_stack))
        elif depth < attr.max_stack:
            stats["depth_under"] += 1
        unreachable = [o for o in instruction_offsets(code, base) if o not in states]
        if unreachable:
            stats["unreachable_methods"] += 1
            stats["unreachable"] += len(unreachable)
        for st in states.values():
            if 2 in st:
                stats["with_cat2"] += 1

class Stats(dict):
    """counters default to 0; KEYS are lists of messages"""
    def __missing__(self, key):
        return 0

def check_chunk(chunk):
    s = Stats({k: [] for k in KEYS})
    for label, raw in chunk:
        s["classes"] += 1
        try:
            check_class(label, raw, s)
        except Exception as e:
            s["errors"].append("%s: parse failed %r" % (label, e))
    return dict(s)

def main(args):
    sample = sample_dir()
    if not args:
        args = [os.path.join(sample, "ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg.jar")]
    elif args == ["--all"]:
        args = sorted(os.path.join(d, f) for d, _, fs in os.walk(sample) for f in fs
                      if f.endswith((".jar", ".class")) and "/extracted/" not in d + "/")
    t0 = time.time()
    total = collections.Counter()
    lists = {k: [] for k in KEYS}
    with multiprocessing.Pool() as pool:
        for s in pool.imap_unordered(check_chunk, olc.chunks(args)):
            for k, v in s.items():
                if k in KEYS: lists[k] += v
                else: total[k] += v
    print("classes %d, methods %d, reachable instructions %d (%d with a long/double on the stack), %.1fs"
          % (total["classes"], total["methods"], total["instructions"], total["with_cat2"], time.time()-t0))
    print("unreachable: %d instructions in %d methods" % (total["unreachable"], total["unreachable_methods"]))
    print("depth < max_stack: %d methods (fine: javac may over-reserve)" % total["depth_under"])
    for k in KEYS:
        print("%s: %d" % (k, len(lists[k])))
        for line in lists[k][:15]: print("   ", line)
    return 0 if not any(lists.values()) else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
