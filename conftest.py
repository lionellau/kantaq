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

# E27-T6 (DEBT-31): an OPT-IN fast Hypothesis profile for a focused local
# property-test loop. Default (env unset) changes nothing — CI and the normal
# loop keep Hypothesis's full example count, so protocol/sync fuzzing rigor is
# untouched where it gates merges. `KANTAQ_HYPOTHESIS_PROFILE=dev` trims the
# default-count property tests (merkle, canonical) for quick iteration; the
# explicit-count sync/domain tests (convergence=25, tracker_fold=40) keep their
# breadth on purpose — we do not tier down sync correctness, even locally.
_hp = os.environ.get("KANTAQ_HYPOTHESIS_PROFILE")
if _hp:
    from hypothesis import HealthCheck, settings

    settings.register_profile(
        "dev",
        max_examples=12,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    settings.register_profile("ci", deadline=None)  # full breadth; no deadline flake under load
    settings.load_profile(_hp)


def pytest_sessionstart(session: object) -> None:
    """Echo the pytest-randomly seed so an order-dependent failure is always
    reproducible — even under `-q` (which skips the normal header) and in CI
    logs. Reproduce a flake with `uv run pytest --randomly-seed=<n>`.

    sessionstart runs regardless of verbosity, after pytest-randomly has resolved
    the seed. Gate to the xdist controller (workers carry `workerinput` and share
    the same seed) so it prints once, not once per worker.
    """
    config = getattr(session, "config", None)
    if config is None or hasattr(config, "workerinput"):
        return
    seed = getattr(getattr(config, "option", None), "randomly_seed", None)
    if seed is not None:
        import sys

        print(f"[kantaq] pytest-randomly seed={seed}", file=sys.stderr)
