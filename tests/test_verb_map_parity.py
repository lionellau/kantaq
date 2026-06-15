"""The collection→write-verb map can never drift between Python and SQL.

The grant-verb scoping (D-03) is expressed twice: in Python
(``kantaq_sync_engine.verify._COLLECTION_WRITE_VERBS``, the client-side verifier)
and in plpgsql (``kantaq.collection_write_verbs`` in ``supabase/rpc/events.sql``,
the v0.2 atomic RPC). If they diverge, the server and client would authorise
different writes — a SEC gap. This gate parses the SQL CASE and pins it equal to
the Python map, so a change in one fails CI until the other follows.
"""

from __future__ import annotations

import re
from pathlib import Path

from kantaq_sync_engine.verify import _COLLECTION_WRITE_VERBS

REPO_ROOT = Path(__file__).resolve().parents[1]
EVENTS_RPC = REPO_ROOT / "supabase" / "rpc" / "events.sql"


def _sql_verb_map() -> dict[str, frozenset[str]]:
    """Parse the ``when '<collection>' then array[...]`` arms of the SQL CASE."""
    sql = EVENTS_RPC.read_text()
    # isolate the collection_write_verbs function body
    body = re.search(
        r"function kantaq\.collection_write_verbs.*?\$\$(.*?)\$\$",
        sql,
        flags=re.S,
    )
    assert body, "kantaq.collection_write_verbs not found in events.sql"
    arms = re.findall(r"when '([a-z_]+)' then array\[([^\]]*)\]", body.group(1))
    return {collection: frozenset(re.findall(r"'([^']+)'", verbs)) for collection, verbs in arms}


def test_sql_verb_map_matches_the_python_verifier() -> None:
    assert _sql_verb_map() == dict(_COLLECTION_WRITE_VERBS)
