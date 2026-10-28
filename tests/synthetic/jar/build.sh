#!/bin/bash
# jvm-15 GUI click-through fixture: app.jar with a Main-Class (app.Main), a README.txt resource and a nested
# lib/helper.jar. In Binary Ninja: File > Load Whole JAR... on app.jar (Main-Class label + preselect, nested-jar
# dialog: Unpack -> app/lib/helper/), then JVM > Open class from this JAR. The jar is committed.
set -e
cd "$(dirname "$0")"
rm -rf build app.jar app
mkdir -p build/classes build/lib build/jar/lib
javac --release 8 -g -Xlint:-options -d build/classes $(find src -name '*.java')
(cd build/classes && jar cf ../jar/lib/helper.jar lib)
printf 'Main-Class: app.Main\n' > build/manifest.txt
echo "a non-class resource" > build/jar/README.txt
cp -r build/classes/app build/jar/
(cd build/jar && jar cfm ../../app.jar ../manifest.txt app lib README.txt)
rm -rf build
unzip -l app.jar
