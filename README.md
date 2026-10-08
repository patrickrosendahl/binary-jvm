# binary-jvm — Java `.class` plugin for Binary Ninja (Python 3)

A Binary Ninja **Architecture + BinaryView** plugin for Java bytecode. It loads `.class`
files (magic `0xCAFEBABE`), parses the full class-file structure (constant pool, fields,
methods, attributes) into Binary Ninja types, registers each method as a function, and
disassembles / partially lifts JVM bytecode to LLIL.

This is a **Python 3 port** of [`Pusty/BinaryNinjaPlugins` → `binary-jvm`](https://github.com/Pusty/BinaryNinjaPlugins)
(original © 2021 Pusty, 0BSD — see `LICENSE`). The upstream plugin targeted the Binary Ninja
**Python 2** API and no longer loads on current versions.

## Status

> **▶ Resumed (2026-10-06).** The pause (jvm-28) was lifted to build Java-level output: operand
> stack as registers (jvm-33), typed signatures + names (jvm-35), compare fusion (jvm-32), exception
> edges inside methods (jvm-42), a Pseudo Java language representation (jvm-43/44) and a class view
> (jvm-41). The plugin is **installed** again (symlink + `files.container.excludedTransforms = ["Universal"]`).

**Python 3 port done; lifter rewritten — every JVM opcode lifts to LLIL.** Tested live in
Binary Ninja 6.1 (via the script bridge, dev-registered names) against ActiveTrader classes:
methods decompile to Java-like HLIL (calls, string constants, static fields, switches,
try/catch handlers). Offline, the decoder + lifter pass over the whole sample (≈47k classes,
11.6M instructions) with no decode failures, no `unimplemented`, and per-instruction stack
effects matching an independent table. All 341 `mdg.jar` classes analyse live in the installed
plugin; invokes decompile as `Class.method(args)` calls (jvm-40).

**Pseudo Java vs Vineflower** (jvm-45…57, 2026-10-07): per-method comparison with Recaf's decompiler on 20
mdg classes -- unnamed stack temporaries 491 → 0 (folded, the rest named from their value), gotos 6 → 0,
`synchronized` blocks instead of comments (14 → 0), `while (true)` loops 9 → 2 (search loops, split
short-circuit conditions, do-while exits), array literals and varargs calls, jsr/ret finally inline, no
exception plumbing outside catch blocks, field initialisers in the class view; lines 2790 → 2360 (Vineflower
1974). Gates in `CLAUDE.md` (testing workflow), details in the wiki page `vineflower-comparison.md`.

See the roadmap below. **This README is the source of truth for the TODO list.**

## Install

Symlink (or copy) this repo's plugin files into the Binary Ninja user plugin directory as a
folder named `binary-jvm`:

```bash
# macOS
ln -s "$(pwd)" ~/Library/Application\ Support/Binary\ Ninja/plugins/binary-jvm
# Linux:   ~/.binaryninja/plugins/binary-jvm
# Windows: %APPDATA%\Binary Ninja\plugins\binary-jvm
```

The repo root is the plugin package (`__init__.py`, `constants.py`, `opcodes.py`,
`classfile.py`, `lifter.py`, `arch.py`, `view.py`, `plugin.json`). Restart Binary Ninja (or
reload plugins). Open a `.class` file → it should be recognized as **JVM Class Format**.

---

## Roadmap / TODO

### 1. Finish the Python 3 port ✅ verified in BN 6.1 (jvm-1)
- [x] `bytes`/`str` fixes in `convert_to_nop`, `invert_branch`, `always_branch`,
      `is_never_branch_patch_available` (data is `bytes`; `data[0]` is an `int`).
- [x] `assemble()` returns `bytes` (BN 6.1 `Architecture._assemble` does
      `memmove(buf, data, len(data))`); build with `bytes([opcode])` / `b"".join(...)`;
      `None` still signals an assemble error.
- [x] `Type.float(w, False)` → `Type.float(w)` (2nd arg is `alternate_name: str`, not a sign).
- [x] `py_compile` clean; no `print`-statement / `iteritems` / `xrange` left.
- [x] **Loaded in BN 6.1 against real `.class` files** (2026-10-08, `ErrorHandler` and
      `Login`): the JVM Class view is selected, every method with code is a named function,
      disassembly renders, and the constant-pool (`pool_N`) and primitive (`primitive_N`)
      symbols are present. The entry point is a real method (jvm-31).

### 2. Opcode / IL coverage ✅ (lifter rewrite done — tickets jvm-2…jvm-11)
All 202 opcodes decode, render and lift; nothing falls through to `unimplemented`.
Design (see `lifter.py` / `arch.py`):
- **Locals are registers** `l<n>` (8 bytes) / `l<n>_lo` (low 4 bytes) for slots 0–63 (covers
  >99.9% of methods; higher `wide` slots fall back to pseudo memory at `0x8000`). The calling
  convention passes arguments in `l0_lo…`, so methods get real parameter lists; returns go in
  `r` (`rh:r` for long/double).
- **Operand stack** is the real stack (`s`), 4-byte slots, long/double take two; operands are
  popped into LLIL temps first (correct operand order; pops inside `if` conditions are avoided
  because BN's stack analysis doesn't see them).
- **Invokes are real calls** (jvm-40): each Methodref/InterfaceMethodref/InvokeDynamic pool entry is
  a data var at its pool pseudo-address typed as a function pointer built from the descriptor
  (receiver first unless only `invokestatic`/`invokedynamic` use it), like an import-table slot; the
  invoke pops its arguments into `a0…` (calling convention `jvm_call`, separate from the locals) and
  lifts as `call([slot])`, the result comes back in `r` / `rh:r`. HLIL reads
  `ValueGetter.getProperty(arg2, "CODEBASE", arg2)` (symbol short name `Class.method`, full JVM name
  as full/raw name). Costs ~2–3× analysis time on call-heavy classes; `INVOKES_AS_CALLS = False` in
  `constants.py` restores the intrinsic form. More than 32 arguments fall back to the intrinsic.
- **Intrinsics** for `getfield/putfield`, `new`, `*newarray`, `arraylength`, `checkcast`,
  `instanceof`, `monitor*`, `athrow`, `fmod` (`frem/drem`).
- **Static fields** are loads/stores of typed data vars at the pool pseudo-address
  `0xF0000000 + idx*8`; `ldc` of int/float/long/double pushes the actual constant, strings/classes
  push a pointer to the pool symbol (renders as `&"text"`).
- **Branches** use labels / `jump(const)`; `tableswitch`/`lookupswitch` lift as compare chains
  (BN recovers `switch` statements). `jsr` is a call that pops its pushed return address,
  `ret` returns through the local. **Catch handlers** become their own functions
  (`<method>$catch_<pc>`); tail-call translation is disabled per view so shared code isn't
  turned into bogus tail calls.
- Decoder fixes: switch padding (relative to the 4-byte-aligned method base) and signed
  keys, `wide iinc` length, MethodHandle `reference_kind` u1, pool tags 17/19/20.
- The raw class file (typed as its structure) is mapped at `0x800000`, not 0 — at 0, every null
  (`const 0`) was typed as a pointer to the class header.

Known cosmetic gaps: `lcmp`/`fcmp*`/`dcmp*` render as bool arithmetic
(`(a > b ? 1 : 0) - (a < b ? 1 : 0) <= 0`); a stack slot reused for a ref and then a long gives
`var.q` accessors; `jsr` subroutines show as `sub_…` calls with the return address argument; a
long/double call result is shown re-assembled as `(retvar:4.d):(retvar.d)`; float/double call
arguments/results are typed as int32/int64 at call sites (`jvm_call` has no float registers).

### 3. JAR support ⬜ (design + implement)
`.jar` = a ZIP of `.class` entries. A `.class`-only loader can't open them directly.
Decide and build a good story — options to weigh:
- **Load one class:** on opening a `.jar`, present a class picker (`get_choice_input`) and
  load the selected entry as the current view.
- **Load all classes:** map every `.class` into one view at distinct base addresses (the
  method-base scheme `0x1000000 + 0x100000*index` already segments per method — extend it
  per class), so cross-class references can resolve.
- A hybrid: index the JAR, default to the main class (from the manifest `Main-Class`), let the
  user add more.
Also handle nested resources and the manifest. Keep the single-`.class` path working.

### 4. Patching — **NOT a goal** ✅ (removed)
Per project decision, interactive patching is out of scope; the ported
`convert_to_nop`/`invert_branch`/`always_branch`/`assemble` paths have been deleted.

### 5. Analysis DB (`.bndb`) must persist renames + notes ⬜
Requirement: a user renaming a function/symbol/variable and adding **comments/notes** must
survive save → close → reopen.
- Symbols are currently created with `define_user_symbol` (constant pool / primitives) and
  `define_auto_symbol` (methods). **Auto symbols are overwritten by re-analysis and user
  renames of them may not stick** — switch method symbols to user symbols (or ensure the
  loader doesn't clobber user edits on DB reopen).
- `init()` re-runs on DB load; guard it so it does not re-`define_*` over user edits.
- Confirm function/variable renames and `set_comment_at` notes round-trip through a saved
  `.bndb`. This is the acceptance test for this item.

---

## Open questions (decide, then record the answer here)

- **Should we support several JVM versions / opcode sets?** **No — one opcode table (jvm-26).**
  The opcode set has been frozen since `invokedynamic` (`0xba`) in Java 7 (class major 51).
  What still changes is the `major_version` header (shown on the class, jvm-25), constant-pool
  tags (`Dynamic`/`Module`/`Package` are not parsed yet), and attributes (`StackMapTable`,
  nest host, `Record`, …) — that work is jvm-21 / jvm-24.

- Should the view show decompiled/source-like output, or is annotated LLIL enough?
- How should constant-pool references render in-line (currently `Pool@N` pseudo-pointers into
  `PSEUDOMEMORY_TABLE`)? Good enough, or resolve to names?

---

## Samples

`sample/` (git-ignored, ~331 MB) holds the **Consorsbank ActiveTrader** macOS app as test
material: **341 `.class` files and 112 `.jar` files** under
`sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/…`. Use these to exercise the
loader, opcode coverage, and JAR handling against real-world (non-toy) bytecode. It is
git-ignored, so it is **not** pushed — regenerate locally with
`cp -R <ActiveTrader.app> sample/ActiveTraderDE_app` if needed.

## Testing notes (important)

This machine runs **Binary Ninja 6.1 Personal**. The **Personal license has no headless
API** — you cannot `import binaryninja` from a standalone interpreter. Test by loading the
plugin in the running GUI and driving it through the **Binary Ninja MCP** tools / the `bnrun`
script bridge (the `binaryninja-mcp` skill is installed globally). See `CLAUDE.md` for the
exact environment details a fresh session needs.

## Attribution

Original author: **Pusty** — <https://github.com/Pusty/BinaryNinjaPlugins>. Licensed 0BSD
(`LICENSE`). This repository is the Python 3 port and continued work.
