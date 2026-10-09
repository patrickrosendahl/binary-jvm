"""Vineflower's own decompiler tests as a second comparison corpus (jvm-77).

Vineflower (~/dev/vineflower, the decompiler Recaf uses) keeps one small Java source per feature under
testData/src/java8/pkg/ (loops, switch, try/finally, try-with-resources, lambdas, anonymous classes, ...),
compiles it with debug info and checks its output against a reviewed reference, testData/results/pkg/<Test>.dec.
SingleClassesTest registers the tests; the Java 8 ones (register(JAVA_8, "TestX")) are used here: mdg is
Java 5 and never shows those constructs.

  python3 tests/vineflower_suite.py prep       # javac --release 8 -g the sources, references -> vf/*.java
  tests/bn_dump_all.sh suite [jvm_devN]        # Pseudo Java of every test class (4 parallel bnrun batches)
  python3 tests/vineflower_suite.py compare [--max K=V ...] [vineflower_compare.py options]

Everything goes to .scratch/vfsuite/ (git-ignored): classes/java8/, vf/pkg/<Test>.java (the reference without
its line-number comments and mapping tables), pj/pkg/<Test>.pj, tests.txt (one pkg/<Test>.class per line).
Only the top-level class of each test is compared (Pseudo Java is per method; inner classes are not dumped).
"""
import os, re, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VINEFLOWER = os.path.expanduser("~/dev/vineflower")
OUT = os.path.join(REPO, ".scratch", "vfsuite")
TESTS = os.path.join(OUT, "tests.txt")
# the current totals: a regression gate like the mdg one (lower them as tickets land)
MAX = ["temps=41", "gotos=16", "labels=14", "sync_comments=18", "offset_stores=0", "while_true=60", "while_true_excess=8", "plumbing=3",
       "dead_code=0", "leaked_catch_var=0", "ref_zero=0", "double_casts=0", "type_mismatch=0", "if_else_assign=3",
       "lost_calls=39", "lost_strings=40", "stmts=6110"]


def java8_tests():
    src = open(os.path.join(VINEFLOWER, "test/org/jetbrains/java/decompiler/SingleClassesTest.java")).read()
    return sorted(set(re.findall(r'register\(JAVA_8, "([\w/]+)"', src)))  # not package-info: no code


def prep():
    classes = os.path.join(OUT, "classes", "java8")
    os.makedirs(classes, exist_ok=True)
    src_dir = os.path.join(VINEFLOWER, "testData/src/java8")
    sources = [os.path.join(d, f) for d, _, fs in os.walk(src_dir) for f in fs if f.endswith(".java")]
    # gradle's JavaCompile default is -g (line numbers, source, local variable tables)
    subprocess.run(["javac", "--release", "8", "-g", "-nowarn", "-Xlint:none", "-d", classes] + sources,
                   check=True, stderr=subprocess.DEVNULL)
    tests = []
    os.makedirs(os.path.join(OUT, "vf", "pkg"), exist_ok=True)
    for t in java8_tests():
        ref = os.path.join(VINEFLOWER, "testData/results/pkg", t + ".dec")
        if not (os.path.exists(ref) and os.path.exists(os.path.join(classes, "pkg", t + ".class"))):
            continue
        out = []
        for line in open(ref, encoding="utf-8"):
            if line.startswith("class '") or line.startswith("Lines mapping:"):
                break  # bytecode / line mapping tables follow the source
            out.append(re.sub(r"\s*//(?: \d+)+\s*$", "", line.rstrip("\n")))  # "// 24" line-number comments
        with open(os.path.join(OUT, "vf", "pkg", t + ".java"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(out).rstrip() + "\n")
        tests.append("pkg/%s.class" % t)
    open(TESTS, "w").write("\n".join(tests) + "\n")
    print("%d Java 8 tests: classes in %s, references in %s" % (len(tests), classes, os.path.join(OUT, "vf")))


def compare(extra):
    args = [sys.executable, os.path.join(REPO, "tests", "vineflower_compare.py"),
            "--classes"] + open(TESTS).read().split() + [
            "--pj", os.path.join(OUT, "pj"), "--vf", os.path.join(OUT, "vf")]
    if "--max" not in extra:
        extra = extra + ["--max"] + MAX
    return subprocess.run(args + extra).returncode


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("prep", "compare"):
        print(__doc__)
        sys.exit(2)
    sys.exit(prep() or 0 if sys.argv[1] == "prep" else compare(sys.argv[2:]))
