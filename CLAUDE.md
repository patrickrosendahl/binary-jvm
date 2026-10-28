# CLAUDE.md

Guidance for Claude Code working in this repo. **The task list / roadmap lives in
[`README.md`](README.md)** ("Roadmap / TODO") — this file is the environment + how-to-work
reference. Read both before starting.

**▶ Resumed (2026-10-06).** The pause (jvm-28) was lifted to build Java-level output: operand
stack as registers (jvm-33), typed signatures + names (jvm-35), compare fusion (jvm-32), exception
edges inside methods (jvm-42), a Pseudo Java language representation (jvm-43/44) and a class view
(jvm-41). The plugin is **installed** again (symlink + `files.container.excludedTransforms = ["Universal"]`).

**▶ Handoff (2026-10-08 evening).** Today: review of the jvm-13 JAR work (jvm-69..76), Vineflower's own Java 8 tests as
a second gate (jvm-77, `tests/vineflower_suite.py`), try-with-resources (jvm-78), nested conditions / condition graphs
(jvm-80), synchronized + loop-condition shapes (jvm-82, partly), finally inside loops (jvm-85). Suite 7241 -> 6417
statements (Vineflower 5680). 2026-10-09: jvm-79 (lambdas), jvm-44 rest (assert, access$, enum/String switches), jvm-87
(unreachable handlers, not a bug) -> 6139 statements; follow-ups jvm-91..94. jvm-86 (finally copies with own locals) and
jvm-84 (multi-catch `catch (A | B e)`) done -> 6097. 2026-10-14 (MacBook clone): jvm-82 round 3/4 (catch in
synchronized, labeled break out of synchronized, loop at the start of synchronized, `L: ..; goto L` as
`while (true)`) -> 6090, gotos 13, labels 10, sync_comments 9; jvm-97 filed (lambda flakiness, lost_calls ±4).
Dump scripts take `JOBS=N` (and `TIMEOUT=S` per batch); second remote `github` (push to both). 2026-10-23/24: jvm-97 (two suite runs identical: `view.analyze_now`, lambda HLIL on demand), jvm-81 (unused local
classes), jvm-82 round 5 (test7/test6/TestEmptyBlocks/sync while(true)), jvm-83 (or-goto condition graphs, TWR return
scope) -> 6264 statements, gotos 7, labels 4. 2026-10-24 (MacBook): jvm-98 (synchronized around an endless loop), jvm-88
(try placed inside a loop it only starts: back edges in try_runs; `finally { if (c) J }` copies) -> 6260; jvm-89 part (tries
nested in a handler body print; +10 statements of real structure -> 6270); jvm-102 (catch nested in a handler body
printed from its own list, exc vars named per clause), jvm-101 (no throwing loop test lifted out of an inner try) -> 6275,
while_true 60; jvm-104 filed (loop break left in a catch). 2026-10-25 (MacBook): jvm-104 (loop break in a catch / break to __propagate), jvm-109 (boolean
ternaries `c ? true : x` -> `c || x`), jvm-108 (`tests/lint.py`, run by the gates), jvm-103 (`skipped` counter) -> 6273;
jvm-110 (two BN segfaults in BNFreeFunction during a suite dump: the dump scripts now drop Function refs + gc.collect()
before closing a view; the rerun was clean). 2026-10-26 (MacBook; ticket comments posted and wiki updated 2026-10-27): jvm-91 items 2/4
(dead String-switch case numbers dropped from `||`, equals results in temporaries, static-field enum subjects), jvm-89
(test6/7/8/9 as finally: empty / non-empty catch at a copy's tail keyed like the dropped rethrow, success-path jump
keeps its call's try, break to __propagate = rethrow, finally exit jump moved into a trailing try), jvm-90 testComplex
(`join` condition graph: `if (c) { L: T } else { ..goto L.. }` -> `if (!c) { .. } T`; now catch (Throwable) without
gotos) -> suite 6226, gotos 4, labels 3; mdg 2014, compare_all 251/251. 2026-10-27 (MacBook): housekeeping (jvm-103/108/110
resolved, jvm-90 rest moved to jvm-99), jvm-89 test12 (synchronized whose tail HLIL put at the loop's end: releases print as
`break`), jvm-91 item 3 (String-switch hash test left apart in a try), jvm-100 (`do { .. } while (c)` from a trailing `if (c)
break;`), jvm-92 part (assert at a loop body's start, concatenated message) -> suite 6215, gotos 3, labels 2, sync_comments 0;
mdg 2014, compare_all 251/251. 2026-10-27/28: jvm-68 (`shared/` split, output identical; binary-cpp took our folding.py /
regions.py back in its 2499ebd, bncpp-88 adds a `fold_skip` hook next), jvm-112 (finally copy in a trailing if of the root
list), jvm-111 (no else after a terminal branch; left inside try/finally and synchronized), `tests/bn_shapes_check.py`
-> suite 6212, mdg 2013, compare_all 251/251. 2026-10-28: bnrun / the shared app replaced by bnengine (jvm-113: `tests/bn_exec.py`, gates
~10 min in all instead of ~1 h). 2026-10-28 (MacBook): jvm-114 item 2 (locals never named after access$/this$/lambda$; an accessor's value is named after its member), jvm-83 (guarded close before a loop's continue / break; `try { try (R r) {..} } catch` -> `try (R r) {..} catch`), jvm-92 (assert whose AssertionError is thrown in a try range), jvm-114 item 1 (lambda bodies inert: no click / rename into the lambda$ function), jvm-15 click-through fixture `tests/synthetic/jar/app.jar` -> suite 6200; follow-ups jvm-115 / jvm-116. Open: jvm-92 rest (AssertionError thrown in a try range), jvm-99 (unreachable finally, test4/5, and
loopInFinally from jvm-90: all paths end in an endless loop), jvm-83 rest left on purpose (testFinallyNested; Vineflower fails too). Under load BN can still hit its analysis time limit (`analysis skipped` in a
dump: redump with RESUME=1). jvm-15 waits for Patrick's GUI
click-through (BN restarted 2026-10-14 with the symlinked plugin, so jarload.py is loaded).

**Project tracking:** tickets live in minitick project **`jvm`** (`jvm-N`) — use the **minitick MCP
tools** (`ticket` / `tickets` / `update` / `create`, registered user-scope; `update("jvm-N", {fields}, comment=…)` resolves with its verification comment in one
call) instead of the CLI (`/Users/patrick/dev/minitick/.venv/bin/minitick … -p jvm`, fallback) —
documentation in miniwiki project
**`jvm`** (`miniwiki tree jvm`; `tickets/` there is a read-only mirror of minitick). There is no
code reviewer: finished tickets go straight to **RESOLVED** with a verification comment. The
project skill **`skills/jvm/SKILL.md`** summarises the workflow and gotchas — load it. `.claude/` is git-ignored;
Claude Code finds the skills through local symlinks, so after a fresh clone run
`mkdir -p .claude/skills && cd .claude/skills && ln -s ../../skills/jvm && ln -s ../../../bn-script-bridge/skills/binaryninja-bridge && ln -s ../../../binary-cpp/skills/bncpp`
(the sister repos sit beside this one under `~/dev`; there is no global bridge skill).

## What this is

A Binary Ninja **Architecture + BinaryView** plugin for Java `.class` bytecode — a **Python 3
port** (and lifter rewrite) of `Pusty/BinaryNinjaPlugins`'s `binary-jvm` (0BSD). The repo root
*is* the plugin package (loaded by BN as package `binary-jvm`, relative imports only):

| file | contents |
| --- | --- |
| `__init__.py` | registers architecture + calling convention + view |
| `constants.py` | `ARCH_NAME`/`VIEW_NAME`, address layout (class file at `0x800000`, method bases, pool pseudo-memory `0xF0000000+idx*8`, `NUM_LOCAL_REGS`, `NUM_ARG_REGS`), `INVOKES_AS_CALLS` |
| `opcodes.py` | opcode tables, `decode_instruction`, descriptor helpers — **pure Python, no BN import** |
| `classfile.py` | class-file parser (builds BN struct types), `JVMClassReader` (pool lookups), reader registry keyed by view handle |
| `lifter.py` | LLIL lifting for every opcode (`InstructionIL`), intrinsics list |
| `arch.py` | `JVM` Architecture (info/text/IL callbacks), registers, calling conventions `jvm` (method args in `l<n>`) and `jvm_call` (invoke call sites, args in `a<n>`) |
| `view.py` | `ClassView` (segments/functions per method, catch handlers, pool symbols, static-field data vars, switch targets, same-class call refs) |
| `plugin.json` | manifest |
| `shared/` | language-neutral parts shared with binary-cpp (HLIL walk, tokens, folding, loop conditions, try regions, condition graphs, compare tooling; jvm-68, see "Code structure") |

## Environment (this machine)

- **Binary Ninja 6.1** — version string `6.1.10815-dev_personal`, **Personal license**, macOS
  (Apple Silicon, `darwin`).
- Binary Ninja runs **in-process** through bn-script-bridge's `bnengine` (since 2026-10-28): scripts run with
  `/Users/patrick/dev/bn-script-bridge/bn script.py` (the app's bundled python; a plain `python3` cannot
  `import binaryninja` under the Personal license). No GUI, bridge or shared app is involved.
- BN app bundle: `/Applications/Binary Ninja.app`
- **Installed API source** (authoritative for this exact version — grep it instead of
  guessing): `/Applications/Binary Ninja.app/Contents/Resources/python/binaryninja/`
  (`architecture.py`, `binaryview.py`, `types.py`, `function.py` are the relevant ones).
- **User plugin directory:** `~/Library/Application Support/Binary Ninja/plugins/`

## Binary Ninja + the skill

- BN work goes through the **`binaryninja-bridge` skill** (bnengine; `.claude/skills/binaryninja-bridge/` symlink into
  `~/dev/bn-script-bridge/skills/`): every script is its own process with its own Binary Ninja, so parallel runs are
  just several processes, and there is no usage lock / lane / job queue any more. `bn --timeout N` kills a run.
- The engine loads the **installed plugin** (the repo symlinked into BN's plugin directory) as package `binary-jvm`
  at start: every run sees the working tree, so there is no dev load. Don't edit plugin files while a gate round
  runs -- each new process loads them.
- Our check scripts run through `tests/bn_exec.py` (starts the engine, NAME=VALUE pairs become the script's globals,
  like bnrun's prepended lines): `bn --timeout 300 tests/bn_exec.py tests/bn_shapes_check.py SHOW=True`.
- Create views explicitly: `BinaryViewType["JVM Class"].create(BinaryView.open(path))`, then
  `view.analyze_now(v)`.
- Engine traps (jvm-113): `update_analysis_and_wait()` works on every thread only since bn-script-bridge d47734e
  (before, on the main thread it logged "UI threads are not permitted to wait" and returned on a half-analysed view:
  `int32_t()` method types, `p1` parameter names); `binaryninja.mainthread.is_main_thread()` is True on *every*
  thread in the engine -- the plugin's UI-thread guard is `_on_ui_thread()` (also Python's main thread).
- **File > Load Whole JAR...** unpacks `foo.jar` to `foo/` beside it and asks which classes to open
  (jvm-13; filter + multi-select, inner classes hidden by default, jvm-76). `Main-Class` from the manifest
  is preselected. Nested `.jar` entries can be unpacked the same way (`lib/bar.jar` → `foo/lib/bar/`, jvm-15);
  other non-class entries are skipped and logged. The decompiler reads the other classes from that folder when it needs them; **JVM > Open
  class from this JAR...** opens one as a tab. Opening one `.class` directly is unchanged.

## Testing workflow (fast → slow)

0. **Lint** (no BN, ~1 s): `python3 tests/lint.py` -- pyflakes over the plugin and `tests/` (`bv`/`bvs`/`env` are builtins
   there, from the bnrun days); 0 findings. `vineflower_compare.py` / `class_view_compare.py` run it with `--max` and fail on a
   finding (jvm-108). A method BN skipped under load counts as `skipped` (gate 0), not as `lost_calls`; the dumps leave
   such a class unstamped, so `RESUME=1` redumps just those (jvm-103).
1. **Offline check first** (no BN, ~1 s): `python3 tests/offline_lift_check.py` stubs `binaryninja`,
   parses `mdg.jar`, decodes + lifts every instruction into a mock IL and checks decode tiling,
   no `unimplemented`, and the per-instruction stack effect against an independent table.
   `--all` runs every jar under `sample/` in parallel (≈47k classes, 11.6M instructions).
   `python3 tests/synthetic_opcode_check.py` lifts the committed rare-opcode class (nop, swap, goto_w, jsr_w, wide).
2. **In BN** (engine, see above): `bn --timeout 300 tests/bn_exec.py tests/bn_batch_check.py` (20 classes from
   `lib/mdg/`), `tests/bn_golden.py` (reference HLIL + readability metrics) the same way.
   **Pseudo Java gates** (after any `pseudo_java.py` change; wiki `vineflower-comparison.md`):
   `python3 tests/vineflower_compare.py --decompile` (once: Vineflower, the decompiler Recaf uses, local fork),
 `tests/bn_dump_all.sh pj` (4 parallel engine processes, ~1 min; jvm-74), then
 `python3 tests/vineflower_compare.py --max temps=0 gotos=0 labels=0 sync_comments=0 offset_stores=0 while_true=1 plumbing=0 dead_code=0 leaked_catch_var=0 lost_calls=0 lost_strings=0 skipped=0 ref_zero=0 double_casts=0 type_mismatch=0 if_else_assign=0 stmts=2013`
 (exit 1 on a regression; `stmts` is the statement count, lower it as tickets land; `--excess N` lists where the
 extra statements against Vineflower come from); and `tests/bn_pseudo_java_compare_all.sh` (~4 min):
 every mdg method with an exception table against HLIL -- must stay 251/251; the script exits non-zero unless every batch
 is ok == with_table (jvm-95). `JOBS=N` sets the number of engine processes (default 4).
 **Vineflower's test corpus** (Java 8 constructs mdg lacks; jvm-77): `python3 tests/vineflower_suite.py prep` (once),
 `tests/bn_dump_all.sh suite` (~2 min), `python3 tests/vineflower_suite.py compare` (gate at the current
 totals; wiki `vineflower-comparison.md` has the per-family table).
 **Class view gate** (after a `classui.py` / class metadata change): `tests/bn_dump_all.sh cv`
 (~1 min), then `python3 tests/class_view_compare.py --max imports=2 headers=0 fields=0 override=0 head=0 throws=0 missing=0`
 (declarations against Vineflower; the 2 imports are types Vineflower names for locals we fold away), and
 `bn --timeout 300 tests/bn_exec.py tests/bn_anon_check.py` (anonymous classes at their `new`, javac 8 + 21; jvm-69;
 exits non-zero on failure, as do the other check scripts -- jvm-94).
 **Statement shapes** (synthetic, seconds): `bn --timeout 300 tests/bn_exec.py tests/bn_shapes_check.py` checks `tests/synthetic/shapes` (jvmtest.Shapes: finally copies in trailing ifs, no else after
 an exit, a lambda in a condition; jvm-111/112) against expected / forbidden snippets; add a method there with each shape fix.
 **Stamps / RESUME / REF** (`tests/gate_state.py`): every dump output carries a stamp (plugin / dump-script / class
 hashes, `<dump>/.stamps.json`; jvm-105); the compare scripts print `WARNING` lines for outputs that are unstamped,
 from several plugin states or not from the working tree (edits during or since the run) -- rerun the
 dump before resolving a ticket on such numbers; `--strict` makes that exit 1. `RESUME=1 tests/bn_dump_all.sh ...` /
 `RESUME=1 tests/bn_pseudo_java_compare_all.sh ...` continue a cut-off run (classes with the current stamp are skipped
 / replayed; jvm-106). Before / after numbers (jvm-107): `--save-ref before` on the compare scripts before a ticket,
 `--ref before` after it prints `REF` + `DELTA` under `TOTAL` (refs in `.scratch/vfcmp/refs/`, `.scratch/vfsuite/refs/`).
### Installing for real
```bash
ln -s /Users/patrick/dev/binary-jvm "$HOME/Library/Application Support/Binary Ninja/plugins/binary-jvm"
```
The engine picks it up at the next run (registration happens at engine start); the GUI app needs a restart. **Installed**
(re-linked 2026-10-06 when the project resumed); a fresh install also needs `files.container.excludedTransforms =
["Universal"]` (jvm-39) for opening `.class` files in the GUI.

## Samples

`sample/` is **git-ignored** (not pushed). It holds the Consorsbank **ActiveTrader** macOS app
(~331 MB) with **341 `.class` + 112 `.jar`** files under
`sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/…` — real-world test bytecode for
loader / opcode-coverage / JAR work. If missing, re-copy an ActiveTrader.app bundle there.
Start with a small standalone `.class` before throwing a big JAR at it.
Favourite target: **`lib/mdg.jar`** (341 classes). **JAR unpack convention** (BN can't open `.jar`
yet, jvm-13): unpack next to the JAR into a dir named like it — `mdg.jar` → `lib/mdg/`
(`unzip -o -q mdg.jar -d mdg`) — and open the `.class` files from there. `sample/extracted/` is a
legacy copy of mdg.jar.

## Code structure: parts shared with sister projects

`../binary-cpp` (readable C++: try/catch from exception tables, HLIL readability passes; a later
binary-dotnet would follow) shares this project's language-neutral parts through a **`shared/` copy in each
repo** (jvm-68) -- no shared package. The one table of shared files is the wiki page **bncpp `code-structure.md`,
"Shared files"** (here | binary-jvm | last sync | differences); there is no SHARED.md. In `shared/` here:
`hlil.py` (HLIL walking), `tokens.py` (TokenMixin: kw / txt / op / type_tok / note, `emit_error`), `folding.py`
(FoldMixin: evaluation-order-safe folding, `x++` values, ternary candidates), `loops.py` (LoopMixin: loop
conditions, `(x = e)` in conditions, split conditions), `regions.py` (`group_try_entries`, `try_runs`; pure),
`condgraph.py` (`cg_reduce`, `cg_verify`; pure) and `compare.py` (statement counting, no-loss checks, gates; pure,
used by `tests/vineflower_compare.py`).
- `shared/` never imports the JVM modules; JVM specifics reach it through hook methods `PseudoJavaFunction`
  overrides (`side_effect`, `reads_memory`, `foldable_var`, `fold_use_ok`, `eval_operands`, `value_parts`,
  `opaque`, `items_of`, `movable`, `loop_exit_special`, …; each mixin's docstring lists its hooks).
- Each shared file carries a provenance header (`# shared with binary-cpp/<path> (synced at <commit>)`); a change
  on one side is ported in the same session or noted in the wiki table, and the other session is told (bncpp).
  Shared passes come with a small synthetic test case where there is one (`tests/pseudo_java_check.py` covers
  regions / condgraph).
- `tests/gate_state.py` hashes `shared/` into the stamps and
  `tests/lint.py` checks it.
- When you add or change a language-neutral part in `pseudo_java.py`, put it in `shared/` with a hook for the
  JVM specifics instead of inlining them into the general logic.

## Conventions

- **Downloaded external documentation goes in `docs/`** (e.g. `docs/java_opcodes.md`,
  `docs/references.md`) — not in chat or a scratch dir. When you fetch a spec page, save it
  there and link it from `docs/references.md`.
- When editing the plugin, grep the installed API source (path above) to confirm a signature
  rather than assuming — the port already relies on several APIs being unchanged from the py2
  era; verify before adding new calls.
- Keep the single-`.class` load path working as you add JAR support.
- **Patching is explicitly out of scope** (README roadmap) — the nop/branch-invert/assemble
  paths have been removed; don't re-add them.

## Git / remote

- Remote (Gitea on the LAN): `ssh://git@mackup.local:222/patrick/binary-jvm.git`
  (web: <http://mackup.local:3000/patrick/binary-jvm>). Default branch `main`.
- Gitea sometimes sleeps; if a push/clone hangs, the host (`mackup.local`, 192.168.1.117) or
  the service may just be down — retry shortly.
- Second remote `github` = `git@github.com:patrickrosendahl/binary-jvm.git`; push `main` to both
  (`git push origin main && git push github main`).
- Commit the plugin + `docs/`; never commit `sample/` (it's git-ignored).
