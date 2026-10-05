# binary-jvm — Java `.class` plugin for Binary Ninja (Python 3)

A Binary Ninja **Architecture + BinaryView** plugin for Java bytecode. It loads `.class`
files (magic `0xCAFEBABE`), parses the full class-file structure (constant pool, fields,
methods, attributes) into Binary Ninja types, registers each method as a function, and
disassembles / partially lifts JVM bytecode to LLIL.

This is a **Python 3 port** of [`Pusty/BinaryNinjaPlugins` → `binary-jvm`](https://github.com/Pusty/BinaryNinjaPlugins)
(original © 2021 Pusty, 0BSD — see `LICENSE`). The upstream plugin targeted the Binary Ninja
**Python 2** API and no longer loads on current versions.

## Status

**Python 3 port done; lifter rewritten — every JVM opcode lifts to LLIL.** Tested live in
Binary Ninja 6.1 (via the script bridge, dev-registered names) against ActiveTrader classes:
methods decompile to Java-like HLIL (calls, string constants, static fields, switches,
try/catch handlers). Offline, the decoder + lifter pass over the whole sample (≈47k classes,
11.6M instructions) with no decode failures, no `unimplemented`, and per-instruction stack
effects matching an independent table. Not yet installed as the real (non-dev) plugin + restart.

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

### 1. Finish the Python 3 port ✅ first pass / ⬜ verified in BN 6.1
- [x] `bytes`/`str` fixes in `convert_to_nop`, `invert_branch`, `always_branch`,
      `is_never_branch_patch_available` (data is `bytes`; `data[0]` is an `int`).
- [x] `assemble()` returns `bytes` (BN 6.1 `Architecture._assemble` does
      `memmove(buf, data, len(data))`); build with `bytes([opcode])` / `b"".join(...)`;
      `None` still signals an assemble error.
- [x] `Type.float(w, False)` → `Type.float(w)` (2nd arg is `alternate_name: str`, not a sign).
- [x] `py_compile` clean; no `print`-statement / `iteritems` / `xrange` left.
- [ ] **Load it in BN 6.1 against a real `.class` and confirm: view recognized, methods
      appear as functions, disassembly + the constant-pool/primitive pseudo-symbols render.**
      (Most of the Type/Structure API — `TypeBuilder.structure()`, `Type.structure_type`,
      `Type.int(w, False, "u1")`, `StructureBuilder.append/packed` — was verified to still
      exist unchanged in 6.1, so this should be close. The fragile spot is
      `JVMStructure.resultingType()`'s type-dedup using `itype.structure(...).members` — the
      `Type.structure` accessor shape may need a tweak; test and adjust.)

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
- **Intrinsics** for `invoke*` (callee pool entry + receiver + args, arg/return sizes from the
  descriptor), `getfield/putfield`, `new`, `*newarray`, `arraylength`, `checkcast`,
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

Known cosmetic gaps: `lcmp`/`fcmp*`/`dcmp*` render as bool arithmetic
(`(a > b ? 1 : 0) - (a < b ? 1 : 0) <= 0`); a stack slot reused for a ref and then a long gives
`var.q` accessors; `jsr` subroutines show as `sub_…` calls with the return address argument.

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

- **Should we support several JVM versions / opcode sets?**
  Analysis: the **opcode set is effectively frozen** — the last new instruction was
  `invokedynamic` (`0xba`) in Java 7 (class major version 51); nothing has been added since.
  So we almost certainly do **not** need per-version opcode tables. Versioning in `.class`
  files lives elsewhere:
  - the `major_version`/`minor_version` header (45=JDK1.1 … 52=Java 8 … 65=Java 21 — table in
    `docs/references.md`); the parser already reads it but doesn't surface it;
  - **constant-pool tags** added over time — `MethodHandle`(15)/`MethodType`(16)/
    `InvokeDynamic`(18) are handled, but `Dynamic`(17, Java 11), `Module`(19) and
    `Package`(20) are **not**, so modern classes will error in `JVMConstantPool.read()`;
  - new **attributes** (`StackMapTable`, `BootstrapMethods`, `NestHost/NestMembers`,
    `Record`, `PermittedSubclasses`, …).
  **Tentative recommendation:** one opcode table, but extend constant-pool tag + attribute
  coverage and display the detected Java version. Confirm and update this section.

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
