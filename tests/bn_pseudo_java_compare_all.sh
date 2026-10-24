#!/bin/bash
# Pseudo Java vs HLIL check (tests/bn_pseudo_java_compare.py) on every mdg method with an exception table:
# 108 classes in 4 parallel bnrun batches (~20-30 min). Prints the four TOTAL lines and every problem.
# Exits non-zero unless every batch reported ok == with_table (a missing TOTAL line means the batch died;
# a method without HLIL counts as a problem since jvm-95).
#   tests/bn_pseudo_java_compare_all.sh [DEV_PKG]     (no argument: the installed plugin)
# Per-class results with stamps go to .scratch/compare_all/classes/ (jvm-105); RESUME=1 replays the classes whose
# result carries the current stamp instead of analysing them again (jvm-106).
set -e
JOBS=${JOBS:-4}   # parallel bnrun batches; above 4 needs scriptbridge.maxParallel >= JOBS
TIMEOUT=${TIMEOUT:-2400}   # per batch, seconds (raise it when the BN instance is shared with heavy jobs)
RESUME=${RESUME:-0}
REPO=/Users/patrick/dev/binary-jvm
OUT=$REPO/.scratch/compare_all
mkdir -p "$OUT"
rm -f "$OUT"/batch_*.txt "$OUT"/run_*.py "$OUT"/result_*.txt
[ "$RESUME" = 1 ] || rm -rf "$OUT/classes"
python3 - "$OUT" "$JOBS" <<'PY'
import subprocess, sys
out = sys.argv[1]
classes = subprocess.run(["python3", "/Users/patrick/dev/binary-jvm/tests/exception_table_classes.py"],
                         capture_output=True, text=True, check=True).stdout.split()
n = int(sys.argv[-1])
for i in range(n):
    open("%s/batch_%d.txt" % (out, i), "w").write("\n".join(classes[i::n]) + "\n")
PY
for i in $(seq 0 $((JOBS - 1))); do
  { [ -n "$1" ] && echo "DEV_PKG=\"$1\""; echo "ONLY_EXC=True"; echo "RESULT_DIR=\"$OUT/classes\""
    [ "$RESUME" = 1 ] && echo "RESUME=True"
    python3 -c "print('CLASSES=' + repr(open('$OUT/batch_$i.txt').read().split()))"
    cat "$REPO/tests/bn_pseudo_java_compare.py"; } > "$OUT/run_$i.py"
  /Users/patrick/dev/bn-script-bridge/bnrun --parallel --timeout $TIMEOUT "$OUT/run_$i.py" > "$OUT/result_$i.txt" 2>&1 &
done
wait
grep -h "TOTAL" "$OUT"/result_*.txt
grep -hv "^####\|TOTAL\|^RESUME\|^   JAVA\|^Traceback\|^  File\|^SystemExit" "$OUT"/result_*.txt || true
status=0
for i in $(seq 0 $((JOBS - 1))); do
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
