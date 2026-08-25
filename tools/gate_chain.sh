#!/bin/bash
# USInv gate chain: waits for shard4 -> merges all shards -> filing-sic -> phase-2-3-build
set -e
cd /home/kerem/USInv
export USINV_EDGAR_EMAIL=keremdemirbas53@gmail.com
PY=.venv/bin/python
PLAN_DIR=transfer/data/local-gate/name-plan-v44-name-discovery/security-bootstrap/discovery/2026-07-17/01158dd986c4868631baabd1987f8a070fdaa9da805f784b8db1b42a4f6a2a46
EV=data/local-gate/cover-evidence-unified
FLAG=data/local-gate/gate-chain-complete.flag

if [ -f "$FLAG" ]; then echo "DONE: already completed"; exit 0; fi

if pgrep -f "sec-cover-bootstrap" > /dev/null; then
    echo "WAIT: cover bootstrap processes still running"
    exit 0
fi

SHARD4=$(find data/local-gate/cover-v44-rebuild-shard4 -name shard.json 2>/dev/null | head -1)
if [ -z "$SHARD4" ]; then
    echo "ACTION_NEEDED: shard4 not running and not published — restart required"
    exit 0
fi

echo "STEP: building unified evidence root"
rm -rf $EV
mkdir -p $EV
for s in $(find data/local-gate/cover-v44-rebuild data/local-gate/cover-v44-rebuild-shard* -name shard.json 2>/dev/null); do
    d=$(dirname $s)
    cp -rL "$d" $EV/$(basename $d)
done
find $EV -name shard.json | wc -l

echo "STEP: sec-cover-merge"
$PY -m usinv sec-cover-merge \
    --discovery-plan $PLAN_DIR \
    --evidence-root $EV

echo "STEP: locating complete evidence"
COMPLETE=$(find data/local-gate -name complete-evidence.json 2>/dev/null | grep -v rebuild-shard | head -1)
echo "complete at: $COMPLETE"
CE_DIR=$(dirname $COMPLETE)

echo "STEP: sec-cover-reconcile"
$PY -m usinv sec-cover-reconcile --cover-evidence $CE_DIR

echo "STEP: edgar-filing-sic-sync (missing-SIC CIK list from v44-final gaps)"
GAPS=/home/kerem/USInv/transfer/data/local-gate/phase-2-3-v44-final/gate-evidence/ff2d93dce898f3e50abf385df3571339293d700f31276810bdacecc97d932705/evidence-gaps.json
CIKS=$($PY - <<PYEOF
import json
g=json.load(open("$GAPS"))
ciks=sorted({str(x["cik"]) for x in g if x["kind"]=="missing_filing_sic"})
print(" ".join("--cik "+c for c in ciks))
PYEOF
)
SIC_OUT=data/local-gate/filing-sic-gatechain
$PY -m usinv edgar-filing-sic-sync \
    --cover-evidence $CE_DIR $CIKS \
    --cache-dir data/local-gate/edgar-cache \
    --output-dir $SIC_OUT

SIC_SNAP=$(find $SIC_OUT -name filing-sic.json | head -1)

echo "STEP: phase-2-3-build"
$PY -m usinv phase-2-3-build \
    --listing-snapshot transfer/data/local-gate/listing/private-listing/alpha-vantage/listing-status/2026-07-17/ead12cd7b99379038d84cdd3bd4b6ec4be94ff04e9fc2f3c05b9e878687d669b \
    --discovery-plan transfer/data/local-gate/ticker-plan-v44-history/security-bootstrap/discovery/2026-07-17/2f5ea78fee9cf2a361e9d472e3fcd6fbf4c50e8e4d242657d35fe705d4367834/discovery.json \
    --cover-evidence $CE_DIR \
    --price-universe transfer/data/local-gate/prices-lifecycle/universe-runs/b1d4f9c410e16bf226f318c34f4eccb9eede62eb94176f9448345169613d1123/price-universe.json \
    --tiingo-lifecycle-zip transfer/data/local-gate/prices-lifecycle/private-lifecycle/supported_tickers.zip \
    --signal-at 2026-07-17T20:00:00+00:00 \
    --fsds-start 2009q1 --fsds-end 2026q1 \
    --archive-dir data/local-gate/fsds-archive \
    --parquet-dir data/local-gate/fsds-parquet \
    --store-dir data/local-gate/pit-store-rebuild \
    --filing-sic-snapshot $SIC_SNAP

touch $FLAG
echo "DONE: gate chain completed successfully"
