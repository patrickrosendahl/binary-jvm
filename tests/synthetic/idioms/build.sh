#!/bin/bash
# Compile the javac-idiom fixtures (jvm-44). --release 8: nested classes reach private members through
# synthetic access$NNN accessors (Java 11+ uses nestmates). Classes are committed so the check needs no JDK.
set -e
cd "$(dirname "$0")"
rm -rf classes
javac --release 8 -g -Xlint:-options -d classes $(find src -name '*.java')
find classes -name '*.class' | sort
