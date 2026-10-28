---
name: jvm
description: Project skill for the binary-jvm Binary Ninja plugin (Java .class Architecture + BinaryView). Use when working on this repo — changing the decoder/lifter/view, testing against the ActiveTrader sample classes (favourite target mdg.jar), loading the plugin into the running Binary Ninja, or updating the jvm tickets (minitick) and wiki (miniwiki).
---

# binary-jvm project workflow

**Resumed 2026-10-06** (after a short pause, jvm-28) — plugin installed. Current work (2026-10-28): Pseudo Java
quality against Vineflower — jvm-99, jvm-93, jvm-92 rest, jvm-82 leftovers (CLAUDE.md handoff).

**Two machines:** MusicMac.local (original; ticket + wiki servers) and MacBook-Pro.local (clone
since 2026-10-12; `/opt/anaconda3/bin/minitick|miniwiki` work as configured). The
git-ignored `sample/`, `.scratch/` and `~/dev/vineflower` must be rsynced from MusicMac (ssh MusicMac -> MacBook
only). Push `main` to both remotes: `git push origin main && git push github main`.

Generic Binary Ninja mechanics (bnengine: BN in the script's own process, the `bn` launcher) are in the global
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
0. `python3 tests/lint.py` (pyflakes, 0 findings; the compare gates run it too, jvm-108). A dump's
   `// analysis skipped` method is `skipped` in the gates, not lost code: `RESUME=1` redumps it (jvm-103).
1. `python3 tests/offline_lift_check.py` (~1–2 s, mdg.jar plus the rare-opcode class; `--all` = every
   sample jar). Must report 0 decode/lift/unimplemented/stack/tiling failures. Run it after every lifter
   change. `python3 tests/synthetic_opcode_check.py` requires nop, swap, goto_w, jsr_w and each wide form.
2. In BN (bnengine, jvm-113): `B=/Users/patrick/dev/bn-script-bridge; $B/bn --timeout 300 tests/bn_exec.py SCRIPT
   [NAME=VALUE ...]` runs a check script (NAME=VALUE = its globals). The engine loads the installed plugin (the repo
   symlink) at every start, so each run sees the working tree: no dev load, no restart, no lock, no lanes; parallel
   = several processes. Don't edit plugin files during a gate round (later processes would load the edit).
   Gates (CLAUDE.md has the `--max` lines): `tests/bn_dump_all.sh pj|cv|suite` (~1 / 1 / 2 min), then
   `vineflower_compare.py` / `class_view_compare.py` / `vineflower_suite.py compare`; `tests/bn_pseudo_java_compare_all.sh`
   (~4 min, 251/251); `bn_exec.py tests/bn_anon_check.py` and `tests/bn_shapes_check.py`. `JOBS=N` engine processes
   (default 4), `TIMEOUT=S` per batch. A run that was cut off continues with `RESUME=1` (jvm-106); heed the compare
   scripts' stamp `WARNING`s (stale / mixed outputs, jvm-105) and use `--save-ref NAME` / `--ref NAME` for a ticket's
   before / after numbers (jvm-107). Run all of them before resolving a ticket that touches `view.py`, `classfile.py`,
   `classui.py` or `pseudo_java.py`, not only the ones for the file you changed.
3. Real install (**installed**): symlink the repo as `plugins/binary-jvm` and set the Universal exclusion (for the
   GUI). The engine needs nothing more; the GUI app needs a restart to see plugin changes.

## Gotchas (learned the hard way)
- **Engine traps (jvm-113):** before bn-script-bridge d47734e, `update_analysis_and_wait()` on the script's main thread
  logged "UI threads are not permitted to wait for analysis completion" and returned on a half-analysed view
  (`int32_t()` method types, `p1` parameters, other folds) -- a dump that looks like that is from a broken engine.
  `is_main_thread()` is True on every thread in the engine: the plugin's guard is `_on_ui_thread()`. A long native
  call can't be interrupted: `bn --timeout N` kills the process (exit 124).
- `bn.load()` picks Raw (or pops a dialog in the GUI) for a view type
  registered after startup — use `BinaryViewType[name].create(BinaryView.open(path))`.
- Engine views are not GUI tabs; open the class in the app for visual checks.
- BN does not track a `pop` inside an `if` condition → always pop into an LLIL temp first.
- Pool pseudo-addresses are `0xF0000000 + idx*8`; static fields need a typed data var there or HLIL
  shows `.d` accessors.
- `.jar` is not opened as a ZIP container by BN's file opener (asked Vector35/Jordan whether `.jar`
  can be registered as a container) — until then, extract classes to test (ticket jvm-13).
