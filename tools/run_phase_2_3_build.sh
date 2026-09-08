#!/bin/bash
# USInv Phase 2.3 build + D032 measurement (post-merge).
# Assumes sec-cover-merge + sec-cover-reconcile already produced
# data/local-gate/cover-evidence-unified/.../complete-evidence.json
set -e
cd /home/kerem/USInv
export USINV_EDGAR_EMAIL=keremdemirbas53@gmail.com
PY=.venv/bin/python

# 1. Locate the complete evidence (merge→reconcile output):
#    cover-evidence-unified/security-bootstrap/complete-evidence/<plan>/<date>/snapshots/<id>/complete-evidence.json
CE_FILE=$(find data/local-gate/cover-evidence-unified -name complete-evidence.json 2>/dev/null | head -1)
COMPLETE_DIR=$(dirname "$CE_FILE")
echo "complete evidence dir: $COMPLETE_DIR"

# 2. Discovery plan directory (must contain discovery.json + manifest.json)
#    v44-name-discovery path:
PLAN_DIR=/home/kerem/USInv/transfer/data/local-gate/name-plan-v44-name-discovery/security-bootstrap/discovery/2026-07-17/01158dd986c4868631baabd1987f8a070fdaa9da805f784b8db1b42a4f6a2a46
echo "discovery plan dir: $PLAN_DIR"

# 3. Price-universe (NEW v44-aligned: v44 cover master + v44-sync snapshots)
NEW_PU=$(ls -td /home/kerem/USInv/data/local-gate/price-universe-v44/universe-runs/*/ 2>/dev/null | head -1)
echo "price-universe: $NEW_PU"

# 3. Run phase-2-3-build (SIC snapshot optional - v44 SIC sync is intentionally
#    out of scope for the local finalize; sector gaps will reflect missing filing-SIC)
echo "STEP: phase-2-3-build"
$PY -m usinv phase-2-3-build \
    --listing-snapshot transfer/data/local-gate/listing/private-listing/alpha-vantage/listing-status/2026-07-17/ead12cd7b99379038d84cdd3bd4b6ec4be94ff04e9fc2f3c05b9e878687d669b \
    --discovery-plan "$PLAN_DIR" \
    --cover-evidence "$COMPLETE_DIR" \
    --price-universe "$NEW_PU" \
    --tiingo-lifecycle-zip transfer/data/local-gate/prices-lifecycle/private-lifecycle/supported_tickers.zip \
    --signal-at 2026-07-17T20:00:00+00:00 \
    --fsds-start 2009q1 --fsds-end 2026q1 \
    --archive-dir data/local-gate/fsds-archive \
    --parquet-dir data/local-gate/fsds-parquet \
    --store-dir data/local-gate/pit-store-rebuild \
    --output-dir data/local-gate/phase-2-3-v44-final-plus 2>&1 | tee data/local-gate/phase-2-3-build.log | tail -60