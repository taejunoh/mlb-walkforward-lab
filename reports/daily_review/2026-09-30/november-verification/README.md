# November 2026 verification evidence

This directory preserves the metadata and native receipt files for the November 9 post-closeout verification. The summary and its interpretation are in the [closeout record](../post-closeout-verification-2026-11-09.json); this archive is supporting provenance, not a new verification or validation claim.

## Contents

- `result.json` — isolated research-loader result.
- `independent_result.json` — independent comparison of game IDs, dates/types, and pitch keys.
- `accepted_native_artifacts.json` and `download_checkpoint.json` — artifact acceptance and acquisition checkpoint metadata.
- `verification.xml` — original offline test output for the recorded 33 tests and 9 subtests.
- `network/` — 45 date-window observation JSON files and `events.jsonl`.
- `history_receipts_postseason_research_v1/` — schedule and Statcast history receipt records and receipt digests.
- `manifests/` — 46 Statcast and one schedule manifest, with their digest sidecars.
- `SHA256SUMS` — checksums for the 380 archived evidence files (not including this index or the checksum file itself).

From this directory, verify the archived file checksums with:

```sh
sha256sum -c SHA256SUMS
```

The archived metadata records original absolute payload paths as historical provenance; those paths are not portable links. The external MLB/Statcast `.raw` and `.csv` payloads are intentionally not included. This metadata archive alone therefore cannot rerun the raw-data reconciliation. Its presence does not change the reported outcomes, and does not indicate that live operations resumed.

The repository's MIT license applies only to original project code and documentation. External data and third-party materials are excluded; see the repository's [license](../../../../LICENSE) and [third-party notices](../../../../THIRD_PARTY_NOTICES.md).
