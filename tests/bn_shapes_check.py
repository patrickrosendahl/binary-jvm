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
    # jvm-92: an assert whose AssertionError is thrown inside a try range (to the handler, not __propagate)
    "assertInTry": (['assert i > 0 : "positive";\nwork(i);'], ["AssertionError", "goto", "__propagate", "exc"]),
    "assertInTryAll": (["assert i > 0;\nwork(i);"], ["AssertionError", "goto", "__propagate", "exc"]),
    # jvm-115: the try-with-resources is the loop body; the break path's close HLIL put behind the loop is implicit
    "twrLoopBreak": (["while (true) {\ntry (FileReader r = new FileReader(f)) {\nif (r.read() < 0) {\nbreak;\n}\nwork(1);\n}\n}"],
                     ["close", "catch", "Throwable"]),
    # jvm-116: try / catch / finally whose catch-all handler HLIL nests in the catch's code, a synchronized block in
    # the catch or the finally: the finally body prints once, its copies after the catch body and the try are gone
    "catchSyncFinally": (["} catch (Throwable t) {\nsynchronized (Shapes.lock) {\nif (Shapes.failure == null) {\n"
                          "Shapes.failure = t;\n}\n}\n} finally {\nif (Shapes.pending.decrementAndGet() == 0) {\n"
                          "synchronized (Shapes.lock) {\nShapes.lock.notifyAll();\n}\n}\n}\n}"],
                         ["on exception", "goto", "label_", "!= 0", "catch (Throwable e)"]),
    "catchFinallyIf": (["work(1);\n} finally {\nif (Shapes.pending.decrementAndGet() == 0) {\n"
                        "work(3);\n}\n}\n}"], ["handler at pc", "work(3);\n}\n}\nif"]),
    "catchFinallyIfSync": (["work(1);\n} finally {\nif (Shapes.pending.decrementAndGet() == 0) {"
                            "\nsynchronized (Shapes.lock) {\nShapes.lock.notifyAll();\n}\n}\n}\n}"],
                           ["on exception", "!= 0", "throw"]),
    # (the catch's `return;` before the method end is jvm-119's redundant return)
    "catchFinallySync": (["work(1);", "} finally {\nsynchronized (Shapes.lock) {\n"
                          "Shapes.lock.notifyAll();\n}\n}\n}"], ["synchronized (Shapes.lock) {\n}"]),
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
# jvm-114: a lambda body printed into its method is inert -- its tokens carry no variable of the lambda$ function --
# while a captured value's tokens are the method's own (rename works there)
for f in v.functions:
    if f.name.split("(")[0].split(".")[-1] != "lambdaInCondition":
        continue
    LV = b.InstructionTextTokenContext.LocalVariableTokenContext
    for line in pj.linear_line_tokens(pj.representation(f), f.hlil.root, None):
        texts = [t.text for t in line]
        if " -> " not in texts:
            continue
        arrow = texts.index(" -> ")
        body = [t for t in line[arrow - 1:] if t.type == b.InstructionTextTokenType.LocalVariableToken]
        own = [t.text for t in body if t.context == LV and t.value]
        if own != ["bottom"]:
            failed += 1
            print("FAIL lambda tokens: locals with a variable identity %r, expected only the capture ['bottom']" % own)
        else:
            print("PASS lambda tokens")
for name in sorted(set(CHECKS) - seen):
    failed += 1
    print("FAIL %s: method not found" % name)
f = None
v.file.close()
print("failed: %d" % failed)
if failed:
    raise SystemExit("%d method(s) failed" % failed)
