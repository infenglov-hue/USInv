# secfsdstools standardizer snapshot

The files under `f_standardize/` are unmodified copies from
`secfsdstools` v2.4.3, upstream commit
`af83c24f999109322d01b4980d207eec67bc749e`:

https://github.com/HansjoergW/sec-fincancial-statement-data-set/tree/v2.4.3/src/secfsdstools/f_standardize

Copyright belongs to the upstream contributors. The snapshot is redistributed
under the included Apache License 2.0. `SOURCE.json` records the SHA-256 of each
copied file so an upstream or local change cannot be mistaken for the reviewed
rule set.

USInv does not import this directory as a replacement package. It pins the
published `secfsdstools==2.4.3` dependency and keeps these standardizer rules as
an auditable bus-factor fallback for the later tag-chain/quarterly phase.

The reviewed upstream release does not implement USInv's required
`Q4 = FY - (Q1 + Q2 + Q3)` derivation. That assumption in the original
blueprint is therefore corrected: USInv's separately versioned contract lives
in `../../rules/quarterly_v1.yaml` and its executable implementation belongs to
Phase 1.4.
