#!/bin/bash
# USInv cover-bootstrap workers for missing CIKs from v44 plan.
# Splits the 143 high-CIK missing set into 3 bounded shards and runs
# them in parallel as separate OS processes. Each process internally
# uses --parallel-ciks=2; 3 workers ~= 6 concurrent REQ total (SEC-safe).
set -e
cd /home/kerem/USInv
export USINV_EDGAR_EMAIL=keremdemirbas53@gmail.com
PY=.venv/bin/python

PLAN="/home/kerem/USInv/transfer/data/local-gate/name-plan-v44-name-discovery/security-bootstrap/discovery/2026-07-17/01158dd986c4868631baabd1987f8a070fdaa9da805f784b8db1b42a4f6a2a46"
ARCHIVE="/home/kerem/USInv/data/local-gate/cover-v44-rebuild-shard7"
OUTPUT="/home/kerem/USInv/data/local-gate/cover-v44-rebuild-shard7/data"
CACHE="/home/kerem/USInv/data/local-gate/edgar-cache"

mkdir -p "$ARCHIVE" "$OUTPUT"

# High-CIK 143 missing, partitioned into 3 contiguous ascending slices.
# Each slice: --start-after-cik (last CIK of previous slice) --max-ciks N.
START_A=1517766   # skip everything <= 1517766; process next ~50
MAX_A=50
START_B=1827090    # mid-boundary from data inspection
MAX_B=50
START_C=1966494    # remainder
MAX_C=60

# Low+mid (1M-1.5M) 19 entries — separate worker since they may fail-open.
START_D=1
MAX_D=20

LOG_DIR=/home/kerem/USInv/data/local-gate/cover-shard7-logs
mkdir -p "$LOG_DIR"

echo "Spawning 4 cover-bootstrap workers..."
( $PY -m usinv sec-cover-bootstrap \
    --discovery-plan "$PLAN" \
    --as-of "2026-07-17T16:00:00-04:00" \
    --avoid-duplicate-binary-cache \
    --reuse-verified-cache \
    --max-filings-per-cik 4 \
    --parallel-ciks 2 \
    --start-after-cik $START_A \
    --max-ciks $MAX_A \
    --archive-dir "$ARCHIVE" \
    --output-dir "$OUTPUT" \
    --cache-dir "$CACHE" \
    > "$LOG_DIR/worker_a.log" 2>&1 ) &
PID_A=$!

( $PY -m usinv sec-cover-bootstrap \
    --discovery-plan "$PLAN" \
    --as-of "2026-07-17T16:00:00-04:00" \
    --avoid-duplicate-binary-cache \
    --reuse-verified-cache \
    --max-filings-per-cik 4 \
    --parallel-ciks 2 \
    --start-after-cik $START_B \
    --max-ciks $MAX_B \
    --archive-dir "$ARCHIVE" \
    --output-dir "$OUTPUT" \
    --cache-dir "$CACHE" \
    > "$LOG_DIR/worker_b.log" 2>&1 ) &
PID_B=$!

( $PY -m usinv sec-cover-bootstrap \
    --discovery-plan "$PLAN" \
    --as-of "2026-07-17T16:00:00-04:00" \
    --avoid-duplicate-binary-cache \
    --reuse-verified-cache \
    --max-filings-per-cik 4 \
    --parallel-ciks 2 \
    --start-after-cik $START_C \
    --max-ciks $MAX_C \
    --archive-dir "$ARCHIVE" \
    --output-dir "$OUTPUT" \
    --cache-dir "$CACHE" \
    > "$LOG_DIR/worker_c.log" 2>&1 ) &
PID_C=$!

# Low-CIK 19 (8 <1M + 11 1M-1.5M) — likely dead shells; do NOT --require-evidence.
( $PY -m usinv sec-cover-bootstrap \
    --discovery-plan "$PLAN" \
    --as-of "2026-07-17T16:00:00-04:00" \
    --avoid-duplicate-binary-cache \
    --reuse-verified-cache \
    --max-filings-per-cik 4 \
    --parallel-ciks 2 \
    --start-after-cik $START_D \
    --max-ciks $MAX_D \
    --archive-dir "$ARCHIVE" \
    --output-dir "$OUTPUT" \
    --cache-dir "$CACHE" \
    > "$LOG_DIR/worker_d_low.log" 2>&1 ) &
PID_D=$!

echo "Workers: A=$PID_A B=$PID_B C=$PID_C D=$PID_D"
echo "Logs: $LOG_DIR/"

wait $PID_A; echo "A done: $?"
wait $PID_B; echo "B done: $?"
wait $PID_C; echo "C done: $?"
wait $PID_D; echo "D done: $?"

echo "ALL COMPLETE"
echo "shard7 accession count: $(ls $ARCHIVE/sec/filing-security/accessions/ 2>/dev/null | wc -l)"
echo "shard.json: $(find $ARCHIVE -name shard.json 2>/dev/null | head -3)"