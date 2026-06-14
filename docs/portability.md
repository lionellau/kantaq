# Portability — export, import, and the round-trip check

kantaq is local-first, and your data is yours. A workspace exports to a single
deterministic `.tar.gz` that any conformant client can re-import. This document
is the **v0.1 manual round-trip procedure** that proves an export is lossless:
export → import into a fresh runtime → re-export → the event logs are
byte-identical, the snapshots are identical, and every blob is still verified by
its content hash (FR-E23-2). The v0.2 CI gate automates exactly this.

## What the bundle contains

A `kantaq-bundle/v1` tarball (the locked MOD-23 layout):

```
manifest.json                      the signed root of trust (per-file sha256 + count)
team_manifest.json                 {team_id, name, collections[]}
actors.ndjson                      members + devices (the Ed25519 verification roots)
grants.ndjson                      canonical, signed CapabilityGrants
collections/<c>/snapshot.ndjson    the deterministic fold
collections/<c>/events.ndjson      canonical signed events, resolution order
blobs/manifest.json                {blob_id: BlobRef}
blobs/data/<blob_id>               raw bytes (content-addressed by sha256)
audit/anchors.json                 the hash-chain anchor over the local trail
audit/policies.json                the audit policy descriptor
```

The manifest records the SHA-256 + byte count of every other file and carries the
exporting device's Ed25519 signature over the canonical manifest, so verifying that
one signature then each file against its recorded hash proves the whole bundle.

## Export your workspace

With the runtime running (`make dev`):

```bash
curl -s -X POST http://127.0.0.1:3939/v1/export \
     -H "Authorization: Bearer $(uv run kantaq token show)" \
     -o kantaq-export.tar.gz
```

`POST /v1/export?since=<rev>` produces an incremental delta (events with
`committed_rev > since`); snapshots are always the full current fold.

## Run the round-trip check

```bash
uv run python scripts/roundtrip_check.py kantaq-export.tar.gz
```

The checker imports the bundle into a throwaway temp runtime, re-exports it, and
compares. It reads only the bundle you hand it and writes only to a temp dir it
deletes — **no workspace data is persisted or committed**. Expected output:

```
checking kantaq-export.tar.gz (… bytes)…
  imported N event(s), M blob(s)
ROUND-TRIP OK: byte-identical event logs, identical snapshots, verified blob hashes.
```

A non-zero exit lists exactly what drifted (which collection's event log or
snapshot, or which blob failed its content-address check).

### Dogfood note (de-identification)

When running this against the real dogfood workspace, treat the bundle as
sensitive: it contains real workspace content. **Do not commit a real bundle, its
contents, or any signed upload URLs** into the repo or fixtures. The automated
test ([`apps/local-runtime/tests/test_import_roundtrip.py`](../apps/local-runtime/tests/test_import_roundtrip.py))
runs the same check on a synthetic, de-identified fixture workspace built in-test,
so CI never sees real data.

## How the importer verifies (fail closed)

The importer ([`kantaq_runtime.import_bundle`](../apps/local-runtime/src/kantaq_runtime/import_bundle.py))
mirrors the backend's verified-ingestion contract (E24-T5):

- the manifest's per-file SHA-256 hashes must match (integrity), and its signature,
  when present, must verify against a device root;
- every **signed** event must verify against its issuing device's root key (found
  via its capability grant) — a tamper is refused; unsigned events are accepted as
  immutable pre-cutover history (D-15);
- every blob must re-hash to its content address on store.

Any failure raises and nothing is half-imported.

## Scope

v0.1 ships the export producer, this importer **library**, the automated fixture
round-trip, and this manual procedure. The public `POST /v1/import` endpoint and
the round-trip **CI gate** are v0.2 (DEBT-03); this importer is the seam they will
call.

## See also

- [protocol.md](protocol.md) — a bundle is a signed event log at rest; the same codec, signatures, and grants verify it.
- [security.md](security.md) — the verified-ingestion contract the importer mirrors.
- [clients/compatibility.md](clients/compatibility.md) — the clients that speak this protocol and the tier they earn.
