# binary-jvm — Java `.class` plugin for Binary Ninja (Python 3)

A Binary Ninja **Architecture + BinaryView** plugin for Java bytecode. It loads `.class` files
(magic `0xCAFEBABE`) and whole JARs, parses the class-file structure (constant pool, fields, methods,
attributes) into Binary Ninja types, makes each method a function, and lifts every JVM opcode to LLIL.
On top of HLIL it adds a **Pseudo Java** language view and a Java-like **class view**.

This is a **Python 3 port and lifter rewrite** of
[`Pusty/BinaryNinjaPlugins` → `binary-jvm`](https://github.com/Pusty/BinaryNinjaPlugins)
(original © 2021 Pusty, 0BSD — see `LICENSE`), which targeted the Python 2 API and no longer loads.

## Features

- **Disassembly and LLIL for all 202 opcodes.** Locals are registers `l<n>`, the operand stack is
  lifted to registers, and invokes are real calls through typed pool slots, so HLIL reads
  `ValueGetter.getProperty(arg2, "CODEBASE", arg2)`. Methods get typed signatures and names.
- **Exception handling.** Catch handlers and exception edges inside methods; Pseudo Java turns them
  into `try`/`catch`/`finally`, `synchronized` and try-with-resources blocks.
- **Pseudo Java.** A language representation (pick *Pseudo Java* in the decompiler view) with folded
  stack temporaries, named locals, loops instead of gotos, array literals, varargs calls and anonymous
  classes at their `new`. Its output is measured against [Vineflower](https://github.com/Vineflower/vineflower)
  (see `CLAUDE.md`, testing workflow).
- **Class view.** **JVM > Show class** renders the class as Java declarations: header, fields with
  initialisers, method signatures, `throws`.
- **JARs.** **File > Load Whole JAR...** (also an open-dialog mode) unpacks `foo.jar` to `foo/` next
  to it, preselects the manifest's `Main-Class`, and lets you pick classes to open; nested `.jar`
  entries can be unpacked the same way. Each class is its own view; the decompiler reads the other
  classes from the folder when it needs them, and **JVM > Open class from this JAR...** opens one.
- Static fields, `ldc` constants, switches (`tableswitch`/`lookupswitch` become `switch`), `jsr`/`ret`
  and Java 7+ constant-pool tags and attributes (`StackMapTable`, `Record`, nest host, ...).

Tested on Binary Ninja 6.1. Offline, the decoder and lifter run over ≈47k real-world classes
(11.6M instructions) with no decode failures and stack effects matching an independent table.

## Install

Symlink (or copy) the repo into the Binary Ninja user plugin directory as `binary-jvm`:

```bash
# macOS
ln -s "$(pwd)" ~/Library/Application\ Support/Binary\ Ninja/plugins/binary-jvm
# Linux:   ~/.binaryninja/plugins/binary-jvm
# Windows: %APPDATA%\Binary Ninja\plugins\binary-jvm
```

Set `files.container.excludedTransforms = ["Universal"]` in the settings (otherwise BN's container
handling grabs `.class` files, whose magic matches a Mach-O universal binary), then restart Binary
Ninja. A `.class` file opens as **JVM Class Format**.

## Roadmap / TODO

Tickets are tracked in minitick project `jvm` (`jvm-N`). Done: Python 3 port, full opcode coverage,
invokes as calls, JAR loading, Pseudo Java, class view. Patching (nop, invert branch, assemble) is
**not a goal** and was removed.

### Analysis DB (`.bndb`) must persist renames + notes ⬜
Renaming a function/symbol/variable and adding comments must survive save → close → reopen.
- Method symbols are `define_auto_symbol`; re-analysis may overwrite user renames — switch to user
  symbols or make sure the loader doesn't clobber them on reopen.
- `init()` re-runs on DB load; guard it so it doesn't re-`define_*` over user edits.
- Acceptance test: function/variable renames and `set_comment_at` notes round-trip through a saved `.bndb`.

### Open questions
- How should constant-pool references render inline — `pool_N` pseudo-pointers, or resolved names?

Decided: one opcode table for all JVM versions (the set is frozen since `invokedynamic` in Java 7;
only header, pool tags and attributes changed); one class per view, not one view per JAR (method
addresses are per view).

## Development

The repo root is the plugin package (`arch.py`, `view.py`, `lifter.py`, `opcodes.py`, `classfile.py`,
`pseudo_java.py`, `classui.py`, `jarload.py`, ...). `tests/offline_lift_check.py` runs without Binary
Ninja; everything else runs inside the GUI through a script bridge. `CLAUDE.md` has the environment,
the test gates and the code layout. Test classes live in `sample/` (git-ignored, not distributed).

## Attribution

Original author: **Pusty** — <https://github.com/Pusty/BinaryNinjaPlugins>. Licensed 0BSD
(`LICENSE`). This repository is the Python 3 port and continued work.
