#!/usr/bin/env python3
"""USInv cover-rebuild progress: exact per-CIK completion from immutable archive manifests.

Reads from ALL shard directories, including the legacy cover-v44-rebuild/sec/.../accessions
path used by shards 1-6 and the direct cover-v44-rebuild-shard7/accessions/ path written
by the post-Aug-25 worker script.
"""

from __future__ import annotations

import glob
import json
import os

PLAN = (
    "/home/kerem/USInv/transfer/data/local-gate/name-plan-v44-name-discovery"
    "/security-bootstrap/discovery/2026-07-17/"
    "01158dd986c4868631baabd1987f8a070fdaa9da805f784b8db1b42a4f6a2a46/discovery.json"
)

OLD_ARCHIVE = "/home/kerem/USInv/data/local-gate/cover-v44-rebuild/sec/filing-security/accessions"
OLD_SHARDS = [
    "/home/kerem/USInv/data/local-gate/cover-v44-rebuild-shard1-early/sec/filing-security/accessions"
] + [
    f"/home/kerem/USInv/data/local-gate/cover-v44-rebuild-shard{i}/sec/filing-security/accessions"
    for i in range(2, 7)
]

# shard7 wrote to a different path (direct accessions/, not sec/filing-security/...)
NEW_SHARD7 = [
    "/home/kerem/USInv/data/local-gate/cover-v44-rebuild-shard7/accessions",
    "/home/kerem/USInv/data/local-gate/cover-v44-rebuild-shard7/sec/filing-security/accessions",
]

ALL_ROOTS = [OLD_ARCHIVE, *OLD_SHARDS, *NEW_SHARD7]

with open(PLAN) as f:
    d = json.load(f)
plan_ciks = sorted({c for r in d["rows"] for c in (r.get("candidate_ciks") or [])})
plan_set = set(plan_ciks)

done_ciks: set[int] = set()
total_accessions = 0
for root in ALL_ROOTS:
    if not os.path.isdir(root):
        continue
    for acc in os.listdir(root):
        ms = glob.glob(f"{root}/{acc}/snapshots/*/manifest.json")
        if not ms:
            continue
        try:
            with open(ms[0]) as f:
                cik = json.load(f).get("cik")
            if cik in plan_set:
                done_ciks.add(cik)
                total_accessions += 1
        except Exception:
            pass

done = len(done_ciks)
pct = round(100 * done / len(plan_ciks), 2)
print(f"EXACT progress: {done}/{len(plan_ciks)} CIKs = {pct}%")
print(f"Total accessions across all shards: {total_accessions}")
print(f"Remaining: {len(plan_ciks) - done}")

# write missing list
missing = sorted(plan_set - done_ciks)
with open("/tmp/cover_missing_ciks.json", "w") as f:
    json.dump(missing, f)
