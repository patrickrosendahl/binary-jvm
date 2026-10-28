# Run inside Binary Ninja via the script bridge (installed plugin, or after tests/bn_dev_load.py):
#   bnrun --parallel --timeout 300 tests/bn_shapes_check.py      (prepend DEV_PKG = "jvm_devN" for a dev load,
#   SHOW = True to print every method)
# Statement shapes the Pseudo Java printer must rebuild (tests/synthetic/shapes, jvmtest.Shapes, built by
# tests/synthetic/shapes/build.sh): per method, snippets the output must contain (whitespace-insensitive) and ones
# it must not. Prints PASS / FAIL per method and exits non-zero when one fails (jvm-94).
import os, re, sys
import binaryninja as b

REPO = "/Users/patrick/dev/binary-jvm"
PKG = globals().get("DEV_PKG") or (b._jvm_dev["pkg"].__name__ if hasattr(b, "_jvm_dev") else "binary-jvm")
SHOW = globals().get("SHOW", False)
# method name -> (expected snippets, forbidden snippets)
CHECKS = {
    # jvm-112: the copy before each branch's implicit return is the finally's, not a catch (Throwable) + copy
    "tailIfFinally": (["} finally {\nwork(2);\n}\n} else {", "} finally {\nwork(1);\n}\n}"],
                      ["catch (Throwable", "__propagate", "}\nwork(2);", "}\nwork(1);"]),
    "nestedTailIfFinally": (["} finally {\nwork(3);\n}"], ["catch (Throwable", "__propagate", "}\nwork(3);"]),
    # jvm-111: the code after a branch that cannot fall through is not an else
    "breakNoElse": (["while (i > 0) {"], ["} else {"]),  # (the loop condition pass gets there first)
    "returnNoElse": (["return 0;\n}"], ["} else {"]),
    "throwNoElse": (['throw new IllegalArgumentException("negative");\n}'], ["} else {"]),
    "syncLoopElse": (["break;\n}\ni--;"], ["} else {"]),
    "loopReturnElse": (["while (i > 0) {"], ["} else {"]),
    # jvm-93: the lambda inline in the if; its capture, a stack temporary HLIL kept instead of the local, is named
    # after that local
    "lambdaInCondition": (["int bottom = list.size() * 2;", "anyMatch(x -> x > bottom)"], ["invokedynamic"]),
    # jvm-93: the bound reference's null check is part of `o::toString`, also inside a try
    "boundRefInTry": (["o::toString;"], ["getClass", "requireNonNull", "invokedynamic"]),
}
pj = sys.modules[PKG + ".pseudo_java"]
vt = b.BinaryViewType[sys.modules[PKG + ".constants"].VIEW_NAME]
squash = lambda t: re.sub(r"[ \t]+", " ", "\n".join(l.strip() for l in t.splitlines() if l.strip()))
v = vt.create(b.BinaryView.open(os.path.join(REPO, "tests/synthetic/shapes/classes/jvmtest/Shapes.class")))
v.update_analysis_and_wait()
getattr(sys.modules[PKG + ".view"], "analyze_now", lambda v: None)(v)  # LVT names before rendering (jvm-97)
failed, seen = 0, set()
for f in sorted(v.functions, key=lambda f: f.start):
    name = f.name.split("(")[0].split(".")[-1]
    if name not in CHECKS:
        continue
    seen.add(name)
    text = "\n".join(pj.render_method(f))
    want, forbid = CHECKS[name]
    flat = squash(text)
    missing = [w for w in want if squash(w) not in flat]
    present = [x for x in forbid if squash(x) in flat]
    if missing or present:
        failed += 1
        print("FAIL %s: missing %r, forbidden %r" % (name, missing, present))
        print(text)
    else:
        print("PASS %s" % name)
        if SHOW:
            print(text)
for name in sorted(set(CHECKS) - seen):
    failed += 1
    print("FAIL %s: method not found" % name)
f = None
v.file.close()
print("failed: %d" % failed)
if failed:
    raise SystemExit("%d method(s) failed" % failed)
