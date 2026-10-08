# External references

Convention: **downloaded external documentation lives in this `docs/` directory** (e.g. [`java_opcodes.md`](java_opcodes.md), mirrored from Wikipedia). When you pull down a spec page or reference for this work, save it here rather than leaving it in chat or a scratch dir.

## Primary specs

- **The Java Virtual Machine Specification** — the authoritative source.
  - Ch. 4, *The `class` File Format*: <https://docs.oracle.com/javase/specs/jvms/se21/html/jvms-4.html>
    (constant pool tags, `access_flags`, fields/methods/attributes, the `major_version` table).
  - Ch. 6, *The JVM Instruction Set*: <https://docs.oracle.com/javase/specs/jvms/se21/html/jvms-6.html>
    (per-opcode operand layout, stack effects, exceptions — the ground truth behind `docs/java_opcodes.md`).
  - Ch. 7, *Opcode Mnemonics by Opcode*: <https://docs.oracle.com/javase/specs/jvms/se21/html/jvms-7.html>

- **Mirrored here:** [`java_opcodes.md`](java_opcodes.md) — complete opcode listing (mnemonic / hex / operand bytes / stack / description), from Wikipedia "List of Java bytecode instructions" (CC BY-SA).

## Binary Ninja API

- Installed API source (read it directly, it is the ground truth for this machine's version):
  `/Applications/Binary Ninja.app/Contents/Resources/python/binaryninja/` — especially
  `architecture.py`, `binaryview.py`, `types.py`, `function.py`.
- Online API docs: <https://api.binary.ninja/> (pin to the matching version if browsing online).
- Example architecture/view plugins to crib from: the official `binja_ebpf`, `binaryninja-msp430`, and the built-in `Z80`/`6502` examples ship as reference BinaryView + Architecture implementations.

## Class-file `major_version` quick table

| major | Java | major | Java |
|---|---|---|---|
| 45 | 1.1 | 58 | 14 |
| 46 | 1.2 | 59 | 15 |
| 47 | 1.3 | 60 | 16 |
| 48 | 1.4 | 61 | 17 |
| 49 | 5 | 62 | 18 |
| 50 | 6 | 63 | 19 |
| 51 | 7 (`invokedynamic`) | 64 | 20 |
| 52 | 8 | 65 | 21 |
| 53 | 9 | 66 | 22 |
| 54 | 10 | 67 | 23 |
| 55 | 11 | 68 | 24 |
| 56 | 12 | 69 | 25 |
| 57 | 13 | | |

From 49 up, the release number is `major - 44`. Minor `65535` (`0xFFFF`) is a preview build of that major (jvm-25).
