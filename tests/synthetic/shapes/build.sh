#!/bin/bash
# Compile the statement-shape fixtures (jvm-111, jvm-112) for tests/bn_shapes_check.py. --release 8 like the
# Vineflower suite; classes are committed so the check needs no JDK.
set -e
cd "$(dirname "$0")"
rm -rf classes
javac --release 8 -g -Xlint:-options -d classes $(find src -name '*.java')
find classes -name '*.class' | sort
