"""Root pytest configuration.

DEBT-18: run the whole suite with the trivial Argon2id profile so token hashing
(MOD-06, `kantaq_core.identity.tokens`) does not dominate runtime — the many
identity / member / API tests each mint or verify a bearer token, and the
production RFC 9106 cost (m=64 MiB, t=3, p=4) is deliberately slow.

This sets the test-only escape hatch read by `tokens._argon2_cost()`. Production
never sets it; `test_tokens.py` asserts the production default is still RFC 9106
with the flag removed. Verification is cost-agnostic (the parameters live in each
PHC string), so a token minted under the fast profile still verifies.

Set before collection, so it applies however pytest is invoked (`make test`,
direct `pytest`, CI). Export `KANTAQ_ARGON2_TEST_FAST=0` to run the suite at the
production cost instead.
"""

import os

os.environ.setdefault("KANTAQ_ARGON2_TEST_FAST", "1")
