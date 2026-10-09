#!/bin/bash
# Pseudo Java vs HLIL check (tests/bn_pseudo_java_compare.py) on every mdg method with an exception table:
# 108 classes in 4 parallel bnrun batches (~20-30 min). Prints the four TOTAL lines and every problem.
# Exits non-zero unless every batch reported ok == with_table (a missing TOTAL line means the batch died;
# a method without HLIL counts as a problem since jvm-95).
#   tests/bn_pseudo_java_compare_all.sh [DEV_PKG]     (no argument: the installed plugin)
set -e
REPO=/Users/patrick/dev/binary-jvm
OUT=$REPO/.scratch/compare_all
mkdir -p "$OUT"
python3 - "$OUT" <<'PY'
import subprocess, sys
out = sys.argv[1]
classes = subprocess.run(["python3", "/Users/patrick/dev/binary-jvm/tests/exception_table_classes.py"],
                         capture_output=True, text=True, check=True).stdout.split()
for i in range(4):
    open("%s/batch_%d.txt" % (out, i), "w").write("\n".join(classes[i::4]) + "\n")
PY
for i in 0 1 2 3; do
  { [ -n "$1" ] && echo "DEV_PKG=\"$1\""; echo "ONLY_EXC=True"
    python3 -c "print('CLASSES=' + repr(open('$OUT/batch_$i.txt').read().split()))"
    cat "$REPO/tests/bn_pseudo_java_compare.py"; } > "$OUT/run_$i.py"
  /Users/patrick/dev/bn-script-bridge/bnrun --parallel --timeout 2400 "$OUT/run_$i.py" > "$OUT/result_$i.txt" 2>&1 &
done
wait
grep -h "TOTAL" "$OUT"/result_*.txt
grep -hv "^####\|TOTAL\|^   JAVA\|^Traceback\|^  File\|^SystemExit" "$OUT"/result_*.txt || true
status=0
for i in 0 1 2 3; do
  totals=$(grep -h "^TOTAL" "$OUT/result_$i.txt" || true)
  if [ -z "$totals" ]; then
    echo "gate: batch $i produced no TOTAL line (script died?)"; status=1; continue
  fi
  ok=$(printf "%s" "$totals" | sed -n "s/.*'ok': \([0-9][0-9]*\).*/\1/p")
  with_table=$(printf "%s" "$totals" | sed -n "s/.*'with_table': \([0-9][0-9]*\).*/\1/p")
  if [ "$ok" != "$with_table" ]; then
    echo "gate: batch $i ok=$ok with_table=$with_table"; status=1
  fi
done
exit $status
