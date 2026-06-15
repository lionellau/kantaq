"""The merge-policy map must be ONE truth in two languages (E05-T2 / MOD-26 §B3).

The per-field conflict scan in the atomic RPC mints a conflict_record only for
``lww`` collections; the Python side (FakeBackend._detect_conflicts, the MOD-28
self-host backend) gates on the SAME policy via kantaq_db.meta.COLLECTION_META.
If the plpgsql ``kantaq.collection_merge_policy`` CASE drifts from meta, the fake
and the real RPC would mint on different collections — a silent split. This gate
parses the SQL CASE and pins it equal to meta, the test_verb_map_parity
discipline applied to the merge policy. (The conflict *logic* itself — that the
RPC mirrors detect_merge — is pinned by conflict_vectors.json on the
EphemeralPostgres cross-check, which needs Postgres; this gate runs anywhere.)
"""

from __future__ import annotations

import re
from pathlib import Path

from kantaq_db.meta import COLLECTION_META

REPO_ROOT = Path(__file__).resolve().parents[1]
EVENTS_RPC = REPO_ROOT / "supabase" / "rpc" / "events.sql"


def _sql_merge_policy() -> dict[str, str]:
    """Parse the ``when '<collection>' then '<policy>'`` arms of the SQL CASE."""
    sql = EVENTS_RPC.read_text()
    body = re.search(
        r"function kantaq\.collection_merge_policy.*?\$\$(.*?)\$\$",
        sql,
        flags=re.S,
    )
    assert body, "kantaq.collection_merge_policy not found in events.sql"
    arms = re.findall(r"when '([a-z_]+)' then '([a-z_]+)'", body.group(1))
    return {collection: policy for collection, policy in arms}


def test_sql_merge_policy_matches_the_python_meta() -> None:
    expected = {name: meta.merge_policy for name, meta in COLLECTION_META.items()}
    assert _sql_merge_policy() == expected
