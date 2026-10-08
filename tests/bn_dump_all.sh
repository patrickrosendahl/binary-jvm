#!/bin/bash
# Pseudo Java or class view dump of the Vineflower comparison classes in 4 parallel bnrun batches (~2-3 min
# instead of 7-12 serialized; jvm-74). Writes .scratch/vfcmp/pj or .scratch/vfcmp/cv like the single scripts.
#   tests/bn_dump_all.sh pj|cv|suite [DEV_PKG]     (no DEV_PKG: the installed plugin)
# then: python3 tests/vineflower_compare.py --max ...   /   python3 tests/class_view_compare.py --max ...
# suite: Pseudo Java of Vineflower's Java 8 test classes (after tests/vineflower_suite.py prep; jvm-77), into
# .scratch/vfsuite/pj; then python3 tests/vineflower_suite.py compare
set -e
REPO=/Users/patrick/dev/binary-jvm
SUITE=$REPO/.scratch/vfsuite
SETTINGS=""
case "$1" in
  pj) SCRIPT=bn_pseudo_java_dump.py; DUMP=$REPO/.scratch/vfcmp/pj ;;
  cv) SCRIPT=bn_class_view_dump.py; DUMP=$REPO/.scratch/vfcmp/cv ;;
  suite) SCRIPT=bn_pseudo_java_dump.py; DUMP=$SUITE/pj
         [ -f "$SUITE/tests.txt" ] || { echo "run python3 tests/vineflower_suite.py prep first" >&2; exit 2; }
         SETTINGS="CLASS_DIR = '$SUITE/classes/java8'; OUT_DIR = '$DUMP'" ;;
  *) echo "usage: $0 pj|cv|suite [DEV_PKG]" >&2; exit 2 ;;
esac
OUT=$REPO/.scratch/dump_all_$1
mkdir -p "$OUT"
rm -rf "$DUMP"
python3 - "$OUT" "$1" <<'PY'
import sys
sys.path.insert(0, "/Users/patrick/dev/binary-jvm/tests")
import vineflower_compare
if sys.argv[2] == "suite":
    classes = open("/Users/patrick/dev/binary-jvm/.scratch/vfsuite/tests.txt").read().split()
else:
    classes = vineflower_compare.DEFAULT_CLASSES
for i in range(4):
    open("%s/batch_%d.txt" % (sys.argv[1], i), "w").write("\n".join(classes[i::4]) + "\n")
PY
for i in 0 1 2 3; do
  { [ -n "$2" ] && echo "DEV_PKG=\"$2\""
    [ -n "$SETTINGS" ] && echo "$SETTINGS"
    python3 -c "print('CLASSES=' + repr(open('$OUT/batch_$i.txt').read().split()))"
    cat "$REPO/tests/$SCRIPT"; } > "$OUT/run_$i.py"
  /Users/patrick/dev/bn-script-bridge/bnrun --parallel --timeout 1800 "$OUT/run_$i.py" > "$OUT/result_$i.txt" 2>&1 &
done
fail=0
for job in $(jobs -p); do wait "$job" || fail=1; done
cat "$OUT"/result_*.txt | grep -v "^package "
exit $fail
