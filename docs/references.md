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
| 45 | 1.1 | 55 | 11 |
| 46 | 1.2 | 56 | 12 |
| 47 | 1.3 | 57 | 13 |
| 48 | 1.4 | 58 | 14 |
| 49 | 5.0 | 59 | 15 |
| 50 | 6 | 60 | 16 |
| 51 | 7 (adds `invokedynamic`) | 61 | 17 |
| 52 | 8 | 62 | 18 |
| 53 | 9 | 65 | 21 |
| 54 | 10 | 67 | 23 |
