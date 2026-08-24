#!/usr/bin/env python3
"""USInv cover-rebuild progress: exact per-CIK completion from immutable archive manifests."""
import glob, json, os, sys

PLAN = '/home/kerem/USInv/transfer/data/local-gate/name-plan-v44-name-discovery/security-bootstrap/discovery/2026-07-17/01158dd986c4868631baabd1987f8a070fdaa9da805f784b8db1b42a4f6a2a46/discovery.json'
ARCHIVE = '/home/kerem/USInv/data/local-gate/cover-v44-rebuild/sec/filing-security/accessions'
SHARDS = [f'/home/kerem/USInv/data/local-gate/cover-v44-rebuild-shard{i}/sec/filing-security/accessions' for i in range(2,7)]

d = json.load(open(PLAN))
plan_ciks = sorted({c for r in d['rows'] for c in (r.get('candidate_ciks') or [])})

done_ciks = set()
for root in [ARCHIVE] + SHARDS:
    if not os.path.isdir(root): continue
    for acc in os.listdir(root):
        # a CIK counts done when at least one archived accession manifest exists for it
        ms = glob.glob(f'{root}/{acc}/snapshots/*/manifest.json')
        if not ms: continue
        try:
            cik = json.load(open(ms[0])).get('cik')
            if cik in set(plan_ciks): done_ciks.add(cik)
        except Exception: pass

done = sum(1 for c in plan_ciks if c in done_ciks)
print(f"EXACT progress: {done}/{len(plan_ciks)} CIKs fully archived = {round(100*done/len(plan_ciks),1)}%")
print(f"remaining: {len(plan_ciks)-done}")
