#!/bin/bash
# Compile the synthetic test classes (jvm-64, jvm-37), committed so the tests do not need a JDK:
#   classes/     --release 8 (class version 52), the oldest target JDK 21's javac still writes
#   classes21/   javac's default target (21, class version 65)
#   rare/        jasm for opcodes javac never emits (nop, swap, goto_w, jsr_w, wide); assembled below
set -e
cd "$(dirname "$0")"
rm -rf classes classes21
javac --release 8 -Xlint:-options -g:none -d classes $(find src -name '*.java')
javac -g:none -d classes21 $(find src -name '*.java')
python3 assemble_jasm.py rare/*.jasm -d classes
find classes classes21 -name '*.class' | sort
