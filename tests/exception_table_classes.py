"""List the .class files under a directory that have at least one method with an exception table
(pure Python: a minimal class-file walk, no Binary Ninja). Used by bn_pseudo_java_compare_all.sh.

usage: python3 tests/exception_table_classes.py [CLASS_DIR]   (default: sample/.../lib/mdg)
"""
import os, struct, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLASS_DIR = os.path.join(REPO, "sample/ActiveTraderDE_app/Contents/WorkingDir/current/lib/mdg")


def has_exception_table(b):
    i = 8
    count = struct.unpack(">H", b[i:i + 2])[0]
    i += 2
    k, utf = 1, {}
    while k < count:
        t = b[i]
        i += 1
        if t == 1:
            n = struct.unpack(">H", b[i:i + 2])[0]
            utf[k] = b[i + 2:i + 2 + n]
            i += 2 + n
        elif t in (3, 4, 9, 10, 11, 12, 17, 18):
            i += 4
        elif t in (5, 6):
            i += 8
            k += 1
        elif t in (7, 8, 16, 19, 20):
            i += 2
        elif t == 15:
            i += 3
        k += 1
    i += 6
    i += 2 + 2 * struct.unpack(">H", b[i:i + 2])[0]
    found = False
    for members in range(2):  # fields, then methods
        n = struct.unpack(">H", b[i:i + 2])[0]
        i += 2
        for _ in range(n):
            i += 6
            ac = struct.unpack(">H", b[i:i + 2])[0]
            i += 2
            for _ in range(ac):
                name, length = struct.unpack(">HI", b[i:i + 6])
                if members == 1 and utf.get(name) == b"Code":
                    code_len = struct.unpack(">I", b[i + 10:i + 14])[0]
                    if struct.unpack(">H", b[i + 14 + code_len:i + 16 + code_len])[0]:
                        found = True
                i += 6 + length
    return found


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else CLASS_DIR
    for d, _, names in sorted(os.walk(root)):
        for n in sorted(names):
            if n.endswith(".class") and has_exception_table(open(os.path.join(d, n), "rb").read()):
                print(os.path.relpath(os.path.join(d, n), root))


if __name__ == "__main__":
    main()
