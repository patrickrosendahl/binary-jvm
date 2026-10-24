---
name: jvm
description: Project skill for the binary-jvm Binary Ninja plugin (Java .class Architecture + BinaryView). Use when working on this repo — changing the decoder/lifter/view, testing against the ActiveTrader sample classes (favourite target mdg.jar), loading the plugin into the running Binary Ninja, or updating the jvm tickets (minitick) and wiki (miniwiki).
---

# binary-jvm project workflow

**Resumed 2026-10-06** (after a short pause, jvm-28) — plugin installed. Current work (2026-10-14): Pseudo Java
quality against Vineflower — jvm-82 rest, then jvm-81, jvm-83, jvm-88..90 (CLAUDE.md handoff has the order).

**Two machines:** MusicMac.local (original; ticket + wiki servers) and MacBook-Pro.local (clone
since 2026-10-12; `/opt/anaconda3/bin/minitick|miniwiki` work as configured). The
git-ignored `sample/`, `.scratch/` and `~/dev/vineflower` must be rsynced from MusicMac (ssh MusicMac -> MacBook
only). Push `main` to both remotes: `git push origin main && git push github main`.

Generic Binary Ninja mechanics (`bnrun` bridge, bundled scripts, usage lock) are in the global
`binaryninja-bridge` skill — load it for any BN work. This skill is the project layer on top.

## Where things live
- **Code**: repo root = plugin package (`__init__.py`, `constants.py`, `opcodes.py` (pure Python),
  `classfile.py`, `lifter.py`, `arch.py`, `view.py`). Layout + environment: `CLAUDE.md`.
- **Roadmap**: `README.md`. **Tickets**: minitick project `jvm` (`jvm-N`) — prefer the
  **minitick MCP tools** (`ticket` / `tickets` / `update` / `create`; one `update` call sets
  fields *and* appends the verification comment; refs like `jvm-84` name the project); CLI
  `/Users/patrick/dev/minitick/.venv/bin/minitick … -p jvm` as fallback. Finished work goes
  straight to `RESOLVED` with a verification comment — there is no code reviewer.
- **Wiki**: miniwiki project `jvm` (`miniwiki tree jvm`): `index`, `plugin-architecture`, `lifting`,
  `testing`, `classfile`, `opcodes/…`, `environment`, `references`. `tickets/` is a read-only mirror
  of minitick — never write there.
- **Samples** (git-ignored): `sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/` —
  favourite target **`mdg.jar`**, unpacked to `lib/mdg/` (see "Loading classes from a JAR").

## Loading classes from a JAR
**File > Load Whole JAR...** unpacks `foo.jar` to `foo/` beside it, preselects `Main-Class`, and can
unpack nested JARs (`lib/bar.jar` → `foo/lib/bar/`). Opening one `.class` is unchanged. The same layout
by hand: `foo.jar` → `foo/`.
```bash
cd <dir with the jar> && unzip -o -q mdg.jar -d mdg      # -> mdg/com/is_teledata/…/X.class
```
Then open the individual `X.class`. Opening a `.class` also needs
`files.container.excludedTransforms = ["Universal"]`, because BN's
Mach-O/Universal handling claims the shared `0xCAFEBABE` magic (jvm-39); BN may still make the
Universal view active — switch to "JVM Class" (GUI: view dropdown; bridge: `--view`).
`tests/offline_lift_check.py --all` skips such `foo/` dirs (their classes are checked via the jar).

## Test loop (fast → slow)
1. `python3 tests/offline_lift_check.py` (~1–2 s, mdg.jar plus the rare-opcode class; `--all` = every
   sample jar). Must report 0 decode/lift/unimplemented/stack/tiling failures. Run it after every lifter
   change. `python3 tests/synthetic_opcode_check.py` requires nop, swap, goto_w, jsr_w and each wide form.
2. Live, no restart: `bnrun tests/bn_dev_load.py` (serialized lane: registers `JVM-devN` /
   `JVM Class devN` globally; BN can't unregister types), then `bnrun --parallel
   tests/bn_batch_check.py` (20 classes), `--parallel tests/bn_golden.py` or a one-class dump. These
   only use their own views: **no usage lock** (it would only block other sessions). The lock is
   needed only for UI tabs (`bv`/`bvs`/`--view`/`--main-thread`).
   Gates (CLAUDE.md has the `--max` lines): `tests/bn_dump_all.sh pj|cv [jvm_devN]` (dumps in 4 parallel
   batches, a few minutes), then `vineflower_compare.py` / `class_view_compare.py`; and
   `tests/bn_pseudo_java_compare_all.sh` (~15 min, background). Java 8 constructs: `tests/vineflower_suite.py prep`, `bn_dump_all.sh suite`,
   `vineflower_suite.py compare` (Vineflower's own tests, jvm-77). The dump scripts take `JOBS=N` (default 4 = bridge
   `scriptbridge.maxParallel`; more batches are slower, GIL-bound). Warn sessions sharing the BN instance
   (binary-cpp) before a gate round — a heavy job alongside made compare_all time out; with the instance shared by
   heavy jobs pass `TIMEOUT=7200` (per batch, dump scripts and compare_all; default 1800 / 2400). A run that was cut
   off continues with `RESUME=1` (jvm-106); heed the compare scripts' stamp `WARNING`s (stale / mixed outputs, jvm-105)
   and use `--save-ref NAME` / `--ref NAME` for a ticket's before / after numbers (jvm-107). Two suite runs are
   identical since jvm-97 (LVT names applied before rendering via `view.analyze_now`, lambda HLIL built on demand). Run all of them before resolving a ticket
   that touches `view.py`, `classfile.py`, `classui.py` or `pseudo_java.py`, not only the ones for the file you changed.
3. Real install (**installed**): symlink the
   repo as `plugins/binary-jvm` and set the Universal exclusion; after changing
   the plugin, restart BN with `/Users/patrick/dev/bn-script-bridge/bnrestart` (saves modified
   views, reopens files, waits for the bridge) — with the user's OK; see the binaryninja-bridge skill.
   A restart (also one for binary-cpp's plugin, which shares the BN instance) drops every dev load and bridge view:
   redo `bn_dev_load.py` before the next gate round. Smoke test of the installed plugin: `tests/bn_pseudo_java_dump.py`
   without `DEV_PKG` (prints `package binary-jvm`) on one or two `CLASSES`, `OUT_DIR` in a scratch dir.

## Gotchas (learned the hard way)
- **bnrun lanes:** serialized by default (one at a time), `--parallel` for own-view scripts (up to 4
  at once). Always use `bnrun --timeout N …`; stop a runaway script with
  `bnrun --cancel` (Ctrl-C on the client also cancels), check with `bnrun --status`. A long native
  call (`update_analysis_and_wait()`) can't be cut short, so keep live batches to a few classes and
  put long runs in background subagents.
- `bn.load()` picks Raw (or pops a dialog in the GUI) for a view type
  registered after startup — use `BinaryViewType[name].create(BinaryView.open(path))`.
- Views created through the bridge are in-process, **not UI tabs**; open in the GUI for visual checks.
- BN does not track a `pop` inside an `if` condition → always pop into an LLIL temp first.
- Pool pseudo-addresses are `0xF0000000 + idx*8`; static fields need a typed data var there or HLIL
  shows `.d` accessors.
- `.jar` is not opened as a ZIP container by BN's file opener (asked Vector35/Jordan whether `.jar`
  can be registered as a container) — until then, extract classes to test (ticket jvm-13).
