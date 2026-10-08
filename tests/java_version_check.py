"""java_version() maps a class-file major/minor to the release name (jvm-25). No Binary Ninja."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from javatypes import java_version

assert java_version(45, 3) == "Java 1.1 (45.3)"
assert java_version(48, 0) == "Java 1.4 (48.0)"
assert java_version(49, 0) == "Java 5 (49.0)"
assert java_version(52, 0) == "Java 8 (52.0)"
assert java_version(65, 0) == "Java 21 (65.0)"
assert java_version(65, 0xFFFF) == "Java 21 preview (65.65535)"
assert java_version(69, 0) == "Java 25 (69.0)"
assert java_version(44, 0) == "class file 44.0"
print("ok")
