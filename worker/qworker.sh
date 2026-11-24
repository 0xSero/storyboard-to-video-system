#!/bin/bash
# Queue worker for one GPU node: runs <root>/queue/*.json one at a time in name order (names start with a
# priority number), each in its own process (worker/gen.py), with a MemAvailable watchdog.
# Done tasks -> <root>/done, failed -> <root>/failed, logs -> <root>/logs/<task>.log. Idles when the queue is empty.
#
# Usage (in tmux so it survives logout):  tmux new -d -s sbq 'bash worker/qworker.sh ~/sbq'
# Env: WANGP_ROOT (default ~/Wan2GP), WANGP_VENV (default ~/venv-wangp), MIN_AVAIL_MB (default 6000)
ROOT=${1:-$HOME/sbq}
HERE=$(cd "$(dirname "$0")" && pwd)
Q=$ROOT/queue; D=$ROOT/done; F=$ROOT/failed; L=$ROOT/logs
mkdir -p "$Q" "$D" "$F" "$L"
# shellcheck disable=SC1091
source "${WANGP_VENV:-$HOME/venv-wangp}/bin/activate"
export CPATH="${WANGP_VENV:-$HOME/venv-wangp}/include:${CPATH:-}"
MIN_AVAIL_MB=${MIN_AVAIL_MB:-6000}
while true; do
  task=$(ls "$Q"/*.json 2>/dev/null | sort | head -1)
  if [ -z "$task" ]; then sleep 20; continue; fi
  name=$(basename "$task" .json)
  echo "=== $(date -u +%FT%TZ) start $name"
  python "$HERE/gen.py" "$task" > "$L/$name.log" 2>&1 &
  pid=$!
  killed=0
  while kill -0 $pid 2>/dev/null; do
    avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
    if [ "$avail" -lt "$MIN_AVAIL_MB" ]; then
      echo "!! low memory ${avail}MB -> kill $name"; kill -9 $pid 2>/dev/null; killed=1; break
    fi
    sleep 10
  done
  wait $pid 2>/dev/null; rc=$?
  if [ $killed = 0 ] && [ $rc = 0 ]; then mv "$task" "$D/" 2>/dev/null; echo "=== $(date -u +%FT%TZ) ok $name"
  else mv "$task" "$F/" 2>/dev/null; echo "=== $(date -u +%FT%TZ) FAIL $name rc=$rc killed=$killed"; fi
  sleep 5
done
