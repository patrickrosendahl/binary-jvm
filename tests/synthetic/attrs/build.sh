#!/bin/bash
# Compile the attribute fixtures (jvm-24). JDK 21's default target emits StackMapTable, NestHost,
# NestMembers, Record and PermittedSubclasses. -parameters / -g add MethodParameters and
# LocalVariableTypeTable. Classes are committed so the check does not need a JDK.
set -e
cd "$(dirname "$0")"
rm -rf classes
javac -parameters -g -d classes $(find src -name '*.java')
find classes -name '*.class' | sort
