# CLAUDE.md

Guidance for Claude Code working in this repo. **The task list / roadmap lives in
[`README.md`](README.md)** ("Roadmap / TODO") — this file is the environment + how-to-work
reference. Read both before starting.

## What this is

A Binary Ninja **Architecture + BinaryView** plugin for Java `.class` bytecode — a **Python 3
port** of `Pusty/BinaryNinjaPlugins`'s `binary-jvm` (0BSD). Single source file: `jvm.py`
(~1560 lines); `__init__.py` just does `from .jvm import *`; `plugin.json` is the manifest.
The upstream was Python-2-only. First-pass port is done (compiles under py3); it still needs
to be loaded and validated inside Binary Ninja — see README TODO #1.

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

## Installing / reloading the plugin for a test

```bash
ln -s /Users/patrick/dev/binary-jvm "$HOME/Library/Application Support/Binary Ninja/plugins/binary-jvm"
```
Then restart Binary Ninja (plugin registration — `JVM.register()` / `ClassView.register()` at
import — happens once at startup; a reload of just the Python plugin is unreliable for
architecture/view registration, so prefer a full restart after editing `jvm.py`).

## Samples

`sample/` is **git-ignored** (not pushed). It holds the Consorsbank **ActiveTrader** macOS app
(~331 MB) with **341 `.class` + 112 `.jar`** files under
`sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/…` — real-world test bytecode for
loader / opcode-coverage / JAR work. If missing, re-copy an ActiveTrader.app bundle there.
Start with a small standalone `.class` before throwing a big JAR at it.

## Conventions

- **Downloaded external documentation goes in `docs/`** (e.g. `docs/java_opcodes.md`,
  `docs/references.md`) — not in chat or a scratch dir. When you fetch a spec page, save it
  there and link it from `docs/references.md`.
- When editing `jvm.py`, grep the installed API source (path above) to confirm a signature
  rather than assuming — the port already relies on several APIs being unchanged from the py2
  era; verify before adding new calls.
- Keep the single-`.class` load path working as you add JAR support.
- **Patching is explicitly out of scope** (README TODO #4) — don't invest in the
  nop/branch-invert/assemble paths; they can be deleted.

## Git / remote

- Remote (Gitea on the LAN): `ssh://git@mackup.local:222/patrick/binary-jvm.git`
  (web: <http://mackup.local:3000/patrick/binary-jvm>). Default branch `main`.
- Gitea sometimes sleeps; if a push/clone hangs, the host (`mackup.local`, 192.168.1.117) or
  the service may just be down — retry shortly.
- Commit the plugin + `docs/`; never commit `sample/` (it's git-ignored).
