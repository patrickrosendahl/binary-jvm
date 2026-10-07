# CLAUDE.md

Guidance for Claude Code working in this repo. **The task list / roadmap lives in
[`README.md`](README.md)** ("Roadmap / TODO") — this file is the environment + how-to-work
reference. Read both before starting.

**▶ Resumed (2026-10-06).** The pause (jvm-28) was lifted to build Java-level output: operand
stack as registers (jvm-33), typed signatures + names (jvm-35), compare fusion (jvm-32), exception
edges inside methods (jvm-42), a Pseudo Java language representation (jvm-43/44) and a class view
(jvm-41). The plugin is **installed** again (symlink + `files.container.excludedTransforms = ["Universal"]`).

**Project tracking:** tickets live in minitick project **`jvm`** (`jvm-N`; CLI
`/Users/patrick/dev/minitick/.venv/bin/minitick … -p jvm`), documentation in miniwiki project
**`jvm`** (`miniwiki tree jvm`; `tickets/` there is a read-only mirror of minitick). There is no
code reviewer: finished tickets go straight to **RESOLVED** with a verification comment. The
project skill **`.claude/skills/jvm/SKILL.md`** summarises the workflow and gotchas — load it.

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
| `view.py` | `ClassView` (segments/functions per method, catch handlers, pool symbols, static-field data vars, switch comments) |
| `plugin.json` | manifest |

## Environment (this machine)

- **Binary Ninja 6.1** — version string `6.1.10815-dev_personal`, **Personal license**, macOS
  (Apple Silicon, `darwin`).
- ⚠️ **Personal license = no headless API.** You cannot `import binaryninja` from a plain
  Python interpreter (that needs a Commercial/Enterprise license). All testing happens in the
  **running GUI**, driven via MCP / the script bridge. Do not try to write a standalone
  `import binaryninja` test harness — it will fail to license.
- BN app bundle: `/Applications/Binary Ninja.app`
- **Installed API source** (authoritative for this exact version — grep it instead of
  guessing): `/Applications/Binary Ninja.app/Contents/Resources/python/binaryninja/`
  (`architecture.py`, `binaryview.py`, `types.py`, `function.py` are the relevant ones).
- **User plugin directory:** `~/Library/Application Support/Binary Ninja/plugins/`

## Binary Ninja MCP + the skill

- The **`binaryninja-mcp` skill is installed globally** (`~/.claude/skills/binaryninja-mcp`),
  so it is available to you automatically — invoke it for any BN work. It covers the `bn_*`
  MCP tools, the `bnrun` full-API script bridge, the shared usage lock across sessions, and
  the Personal-license workarounds.
- Typical loop: load a `.class` in the GUI → `bn_binary_view_list` → `bn_binary_view_set_active`
  → inspect with `bn_function_list` / `bn_function_disassembly` / `bn_function_il` /
  `bn_string_list` / `bn_symbol_list`. For anything the `bn_*` tools don't cover (defining
  types, registering architectures, poking the view during `init`), use the `bnrun` bridge for
  the full Python API inside the app.
- ⚠️ A view type registered after startup (dev loads) is **not** picked by `bn.load()` or MCP
  `bn_open_item_open` (Raw / a GUI dialog instead) — create it explicitly with
  `BinaryViewType[name].create(BinaryView.open(path))`. Such views are not UI tabs.
- BN's file opener does **not** treat `.jar` as a ZIP container (asked Vector35/Jordan whether
  `.jar` can be registered as a container) — extract classes for testing; see ticket jvm-13.

## Testing workflow (fast → slow)

1. **Offline check first** (no BN, ~1 s): `python3 tests/offline_lift_check.py` stubs `binaryninja`,
   parses `mdg.jar`, decodes + lifts every instruction into a mock IL and checks decode tiling,
   no `unimplemented`, and the per-instruction stack effect against an independent table.
   `--all` runs every jar under `sample/` in parallel (≈47k classes, 11.6M instructions).
2. **Live in BN without restart**: `bnrun tests/bn_dev_load.py` copies the package to
   `/tmp/jvm_devN/`, renames arch/view to `JVM-devN` / `JVM Class devN` and registers them
   (BN cannot unregister types, so every reload needs fresh names; older dev views are disabled).
   Then `bnrun --parallel tests/bn_batch_check.py` (20 classes from `lib/mdg/` by default, per-class
   timing); `bnrun --parallel tests/bn_golden.py` dumps reference HLIL + readability metrics. Views created this way are in-process but **not UI tabs**.
   **Pseudo Java gates** (after any `pseudo_java.py` change; wiki `vineflower-comparison.md`):
   `python3 tests/vineflower_compare.py --decompile` (once: Vineflower, the decompiler Recaf uses, local fork),
   `bnrun --parallel --timeout 600 tests/bn_pseudo_java_dump.py` (prepend `DEV_PKG = "jvm_devN"` for a dev load), then
   `python3 tests/vineflower_compare.py --max temps=210 gotos=0 labels=0 sync_comments=5 offset_stores=0 while_true=8 plumbing=0 dead_code=0 leaked_catch_var=0 lost_calls=0 lost_strings=0`
   (exit 1 on a regression); and `tests/bn_pseudo_java_compare_all.sh [jvm_devN]` (~25 min, run it in the
   background): every mdg method with an exception table against HLIL -- must stay 251/251.
3. bnrun has two lanes: serialized (default; UI work and global registration like `bn_dev_load.py`)
   and `--parallel` (scripts on their own views, up to 4 at once, no lock). Always pass **`--timeout N`**; a running script can be stopped
   with `bnrun --cancel` (or Ctrl-C on the client), `bnrun --status` shows what is running.
   Cancellation lands between Python bytecodes — a long native call (`update_analysis_and_wait()`)
   finishes first — so keep batches small. The usage lock is only needed for UI tabs
   (`bv`/`bvs`/`--view`/`--main-thread` are refused without it).

### Installing for real
```bash
ln -s /Users/patrick/dev/binary-jvm "$HOME/Library/Application Support/Binary Ninja/plugins/binary-jvm"
```
Then restart Binary Ninja (architecture/view registration happens once at startup) — with the
user's OK via `/Users/patrick/dev/bn-script-bridge/bnrestart` (saves modified views, reopens files,
waits for the bridge). **Installed** (re-linked 2026-10-06 when the project resumed); a fresh install also needs `files.container.excludedTransforms = ["Universal"]` (jvm-39).

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

## Conventions

- **Downloaded external documentation goes in `docs/`** (e.g. `docs/java_opcodes.md`,
  `docs/references.md`) — not in chat or a scratch dir. When you fetch a spec page, save it
  there and link it from `docs/references.md`.
- When editing the plugin, grep the installed API source (path above) to confirm a signature
  rather than assuming — the port already relies on several APIs being unchanged from the py2
  era; verify before adding new calls.
- Keep the single-`.class` load path working as you add JAR support.
- **Patching is explicitly out of scope** (README TODO #4) — the nop/branch-invert/assemble
  paths have been removed; don't re-add them.

## Git / remote

- Remote (Gitea on the LAN): `ssh://git@mackup.local:222/patrick/binary-jvm.git`
  (web: <http://mackup.local:3000/patrick/binary-jvm>). Default branch `main`.
- Gitea sometimes sleeps; if a push/clone hangs, the host (`mackup.local`, 192.168.1.117) or
  the service may just be down — retry shortly.
- Commit the plugin + `docs/`; never commit `sample/` (it's git-ignored).
