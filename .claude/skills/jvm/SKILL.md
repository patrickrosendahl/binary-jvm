---
name: jvm
description: Project skill for the binary-jvm Binary Ninja plugin (Java .class Architecture + BinaryView). Use when working on this repo — changing the decoder/lifter/view, testing against the ActiveTrader sample classes (favourite target mdg.jar), loading the plugin into the running Binary Ninja, or updating the jvm tickets (minitick) and wiki (miniwiki).
---

# binary-jvm project workflow

Generic Binary Ninja mechanics (MCP tools, `bnrun` bridge, usage lock) are in the global
`binaryninja-mcp` skill — load it for any BN work. This skill is the project layer on top.

## Where things live
- **Code**: repo root = plugin package (`__init__.py`, `constants.py`, `opcodes.py` (pure Python),
  `classfile.py`, `lifter.py`, `arch.py`, `view.py`). Layout + environment: `CLAUDE.md`.
- **Roadmap**: `README.md`. **Tickets**: minitick project `jvm` (`jvm-N`), CLI
  `/Users/patrick/dev/minitick/.venv/bin/minitick … -p jvm`. Finished work goes straight to
  `RESOLVED` with a verification comment — there is no code reviewer.
- **Wiki**: miniwiki project `jvm` (`miniwiki tree jvm`): `index`, `plugin-architecture`, `lifting`,
  `testing`, `classfile`, `opcodes/…`, `environment`, `references`. `tickets/` is a read-only mirror
  of minitick — never write there.
- **Samples** (git-ignored): `sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/` —
  favourite target **`mdg.jar`**, extracted to `sample/extracted/` for single-class loading.

## Test loop (fast → slow)
1. `python3 tests/offline_lift_check.py` (~1–2 s, mdg.jar; `--all` = every sample jar). Must
   report 0 decode/lift/unimplemented/stack/tiling failures. Run it after every lifter change.
2. Live, no restart: take the lock (`bnrun --lock --purpose …`), `bnrun tests/bn_dev_load.py`
   (registers `JVM-devN` / `JVM Class devN`; BN can't unregister types), then
   `bnrun tests/bn_batch_check.py` (20 classes) or a one-class dump. Release the lock after.
3. Real install: symlink the repo into the BN plugins dir and ask the user to restart BN.

## Gotchas (learned the hard way)
- **bnrun scripts run one at a time.** Always use `bnrun --timeout N …`; stop a runaway script with
  `bnrun --cancel` (Ctrl-C on the client also cancels), check with `bnrun --status`. A long native
  call (`update_analysis_and_wait()`) can't be cut short, so keep live batches to a few classes and
  put long runs in background subagents.
- `bn.load()` / MCP `bn_open_item_open` pick Raw (or pop a dialog in the GUI) for a view type
  registered after startup — use `BinaryViewType[name].create(BinaryView.open(path))`.
- Views created through the bridge are in-process, **not UI tabs**; open in the GUI for visual checks.
- BN does not track a `pop` inside an `if` condition → always pop into an LLIL temp first.
- Pool pseudo-addresses are `0xF0000000 + idx*8`; static fields need a typed data var there or HLIL
  shows `.d` accessors.
- `.jar` is not opened as a ZIP container by BN's file opener (asked Vector35/Jordan whether `.jar`
  can be registered as a container) — until then, extract classes to test (ticket jvm-13).
