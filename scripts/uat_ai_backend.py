#!/usr/bin/env python
"""AI-backend UAT runner — the "試錯" (trial-and-error / abuse) matrix.

Where ``scripts/compat_check.py`` proves the *happy* Tier-1 contract (a real MCP
SDK client connects, reads, proposes, gets approved), this runner proves the
**adversarial** contract: every way a compromised or buggy agent can poke the AI
backend (the MCP gateway + tools) is *bounded, denied, and audited*. It is the
acceptance-level view of the security suite — the same tests CI runs
(``packages/mcp/tests`` red-team / injection / gateway-denial / grant-session),
collected and grouped into the seven UAT-A categories of the sprint 1-7 UAT plan
(docs repo ``docs/test/sprint-1-7-uat-plan.md``), with a matrix-ready summary and
a markdown report.

    uv run python scripts/uat_ai_backend.py                 # print the matrix
    uv run python scripts/uat_ai_backend.py --report out.md # also write a report

It runs against the **real gateway and codec** (not mocks) on a hermetic SQLite
arena — the same isolation the dry-run used (no Supabase, no real workspace). It
exits non-zero on any failure OR any drift (a mapped case with no matching test),
so the UAT matrix can never quietly outrun the executed tests.

Every row maps to a pytest node so the evidence is reproducible one test at a
time, and to the FR/NFR id it satisfies (dev-planning.md) — cite these in the
results report.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_TESTS = ROOT / "packages" / "mcp" / "tests"

# The five suites that exercise the adversarial AI-backend surface. Kept as a
# tuple so the runner and the plan doc name exactly the same files.
SUITES: tuple[Path, ...] = (
    MCP_TESTS / "test_gateway_denials.py",
    MCP_TESTS / "test_gateway_grant_sessions.py",
    MCP_TESTS / "test_injection.py",
    MCP_TESTS / "test_injection_corpus.py",
    MCP_TESTS / "test_red_team.py",
)


@dataclass(frozen=True)
class Case:
    """One UAT-A row: an adversarial property, the test that proves it, and the
    requirement it satisfies. ``node`` is matched as a substring of the pytest
    node id, so a parametrized test (many node ids) rolls up into one case."""

    uid: str
    title: str
    expect: str  # the deny reason / bounded behaviour the case asserts
    trace: str  # FR/NFR id(s) from dev-planning.md
    node: str  # pytest function name (substring match against the node id)


@dataclass
class Category:
    key: str
    title: str
    cases: list[Case] = field(default_factory=list)


# The seven 試錯 categories. Order is the story we tell in the report: get in,
# get authorized, don't flood, don't execute injected text, don't read what's
# withheld, don't escalate/skip the human, and prove revocation + the audit.
CATEGORIES: tuple[Category, ...] = (
    Category(
        "A1",
        "Connection & identity",
        [
            Case(
                "A1.1",
                "A token that does not match the session is denied",
                "deny: identity",
                "FR-E09-3 / NFR-E09-1",
                "test_identity_check_token_must_match_the_session",
            ),
            Case(
                "A1.2",
                "An expired session denies and keeps denying",
                "deny: expiry",
                "FR-E09-3",
                "test_expiry_check_denies_and_keeps_denying",
            ),
            Case(
                "A1.3",
                "A bad agent role refuses to derive a session",
                "refused at derivation",
                "FR-E06-4 / FR-E09-2",
                "test_bad_agent_role_refuses_to_derive_a_session",
            ),
            Case(
                "A1.4",
                "A grant derives scope/tools/write-mode/expiry/policy",
                "session shaped by grant",
                "FR-E09-2",
                "test_grant_session_derives_scope_tools_writemode_expiry_policy",
            ),
            Case(
                "A1.5",
                "A read-only grant yields a read-only session",
                "write_mode = read_only",
                "FR-E09-4",
                "test_read_only_grant_yields_a_read_only_session",
            ),
            Case(
                "A1.6",
                "session/init describes the grant session",
                "descriptor honest",
                "FR-E09-2",
                "test_session_init_descriptor_describes_the_grant_session",
            ),
            Case(
                "A1.7",
                "session/init HTTP endpoint derives a session",
                "endpoint contract",
                "FR-E09-1",
                "test_session_init_http_endpoint",
            ),
        ],
    ),
    Category(
        "A2",
        "Authorization — the eight per-call checks",
        [
            Case(
                "A2.1",
                "Out-of-scope & invented tools are denied",
                "deny: tool_allowlist",
                "FR-E08-2 / FR-E09-3",
                "test_tool_allowlist_denies_out_of_scope_and_unknown_tools",
            ),
            Case(
                "A2.2",
                "Propose is denied even if the tool is allowlisted (read-only)",
                "deny: write_mode",
                "FR-E09-4",
                "test_write_mode_check_denies_propose_even_if_allowlisted",
            ),
            Case(
                "A2.3",
                "A tool outside the grant's resource is denied",
                "deny: collection_scope",
                "FR-E09-3 / NFR-E06-3",
                "test_collection_scope_denies_a_tool_outside_the_grant_resource",
            ),
            Case(
                "A2.4",
                "A verb the grant lacks is denied (defense in depth)",
                "deny: verb_match",
                "FR-E09-3",
                "test_verb_match_denies_when_the_grant_lacks_the_capability",
            ),
            Case(
                "A2.5",
                "An unknown audit policy is denied",
                "deny: audit_policy",
                "FR-E07-2 / FR-E09-3",
                "test_audit_policy_denies_an_unknown_policy",
            ),
        ],
    ),
    Category(
        "A3",
        "Rate limits & bulk-write containment",
        [
            Case(
                "A3.1",
                "50/min trips the rate limit, kills the session, audits",
                "deny: rate_limit + kill",
                "FR-E08-4",
                "test_rate_limit_kills_the_session_and_audits",
            ),
            Case(
                "A3.2",
                "The 500/session lifetime cap kills too",
                "deny: rate_limit + kill",
                "FR-E08-4",
                "test_session_lifetime_cap_kills_too",
            ),
            Case(
                "A3.3",
                "A proposal flood trips the limit; kill stays dead",
                "deny: rate_limit",
                "NFR-E08-1",
                "test_bulk_rate_limit_flood_kills_the_session",
            ),
            Case(
                "A3.4",
                "No mutate tool accepts a list of ids (structural)",
                "no bulk surface",
                "FR-E08-5 / DEBT-24",
                "test_no_bulk_mutate_tool_exists",
            ),
            Case(
                "A3.5",
                "A single proposal changes nothing until a human approves",
                "bounded (applied=false)",
                "FR-E08-3",
                "test_bulk_single_proposal_is_bounded",
            ),
        ],
    ),
    Category(
        "A4",
        "Prompt-injection containment (untrusted-content fencing)",
        [
            Case(
                "A4.1",
                "A planted instruction returns fenced, never executed",
                "fenced data",
                "FR-E08-1",
                "test_planted_instruction_returns_fenced_not_executed",
            ),
            Case(
                "A4.2",
                "Hostile title & labels are fenced too",
                "fenced data",
                "FR-E08-1",
                "test_hostile_title_and_labels_are_fenced_too",
            ),
            Case(
                "A4.3",
                "Every corpus payload is fenced in every tracker string",
                "marker never drops",
                "FR-E08-8",
                "test_payload_is_fenced_in_every_tracker_string",
            ),
            Case(
                "A4.4",
                "Payloads are fenced in memory & context bundles",
                "marker never drops",
                "FR-E08-8",
                "test_payload_is_fenced_in_memory_and_context",
            ),
            Case(
                "A4.5",
                "A comment body is fenced on echo",
                "marker never drops",
                "FR-E08-8",
                "test_comment_body_is_fenced_on_echo",
            ),
            Case(
                "A4.6",
                "A compromised agent cannot exceed its session via content",
                "bounded",
                "NFR-E08-1",
                "test_a_compromised_agent_cannot_exceed_its_session",
            ),
            Case(
                "A4.7",
                "The whole corpus is fenced for a malicious session",
                "marker never drops",
                "FR-E08-8 / NFR-E08-1",
                "test_planted_injection_is_fenced_for_the_malicious_session",
            ),
        ],
    ),
    Category(
        "A5",
        "Exfiltration — memory policy on every read",
        [
            Case(
                "A5.1",
                "Private/out-of-scope/stale memory is withheld",
                "deny: memory_policy",
                "NFR-E16-1 / FR-E16-4",
                "test_exfil_withheld_memory_is_denied",
            ),
            Case(
                "A5.2",
                "A role-less agent cannot read memory at all",
                "deny: memory_policy",
                "FR-E16-4",
                "test_exfil_memory_without_a_role",
            ),
            Case(
                "A5.3",
                "Read-via-write: promote of an out-of-scope note is denied",
                "deny: memory_policy",
                "NFR-E16-1",
                "test_exfil_out_of_scope_memory_via_promote",
            ),
            Case(
                "A5.4",
                "A tickets-only grant cannot reach memory",
                "deny: collection_scope",
                "FR-E09-3",
                "test_exfil_cross_collection_read",
            ),
            Case(
                "A5.5",
                "Preview never leaks a private memory id",
                "id never surfaces",
                "NFR-E16-1",
                "test_exfil_preview_never_leaks_a_private_memory_id",
            ),
        ],
    ),
    Category(
        "A6",
        "Escalation & queue-skipping (no write without a human)",
        [
            Case(
                "A6.1",
                "A propose-only agent cannot reach approve",
                "deny: tool_allowlist",
                "NFR-E08-1 / FR-E08-3",
                "test_escalate_approve_own_proposal",
            ),
            Case(
                "A6.2",
                "Invented & audit-log tools never exist",
                "deny: tool_allowlist",
                "FR-E08-2",
                "test_escalate_forged_and_audit_tools",
            ),
            Case(
                "A6.3",
                "Allowlist drift to approve is still caught by verb-match",
                "deny: verb_match",
                "FR-E09-3",
                "test_escalate_verb_drift_defense_in_depth",
            ),
            Case(
                "A6.4",
                "A code_agent cannot resolve a richer role's context",
                "deny: memory_policy",
                "NFR-E16-1",
                "test_escalate_cross_role_context",
            ),
            Case(
                "A6.5",
                "Binding to someone else's grant is an identity denial",
                "deny: identity",
                "FR-E06-4",
                "test_escalate_foreign_grant_session",
            ),
            Case(
                "A6.6",
                "A tampered subject role fails closed to the 24h ceiling",
                "deny: identity",
                "NFR-E06-2 / FR-E06-5",
                "test_escalate_tampered_role_lifts_ceiling",
            ),
            Case(
                "A6.7",
                "An agent grant cannot be crafted with an owner-tier lifetime",
                "refused at issuance",
                "FR-E06-5",
                "test_escalate_craft_owner_tier_grant",
            ),
            Case(
                "A6.8",
                "A read-only session's direct propose is refused",
                "deny: write_mode",
                "FR-E09-4",
                "test_queue_skip_write_mode_direct",
            ),
            Case(
                "A6.9",
                "Propose-then-self-approve in one session is denied",
                "deny: tool_allowlist",
                "NFR-E08-1 / FR-E08-3",
                "test_queue_skip_propose_then_self_approve",
            ),
        ],
    ),
    Category(
        "A7",
        "Revocation timing & audit completeness",
        [
            Case(
                "A7.1",
                "Revoking the grant stops the derived session",
                "next call denied",
                "FR-E06-6",
                "test_revoking_the_grant_stops_the_derived_session",
            ),
            Case(
                "A7.2",
                "Revocation stops the session within the <5s budget",
                "<5s wall-clock",
                "NFR-E06-2",
                "test_revocation_stops_the_session_within_the_wall_clock_budget",
            ),
            Case(
                "A7.3",
                "A full malicious session ends with zero scope escapes",
                "ticket unmoved + audited",
                "NFR-E08-1",
                "test_red_team_session_ends_with_zero_scope_escapes",
            ),
            Case(
                "A7.4",
                "The manifest covers all four attack classes",
                "coverage invariant",
                "NFR-E08-1",
                "test_manifest_covers_all_four_attack_classes",
            ),
            Case(
                "A7.5",
                "Every catalog attack is actually exercised",
                "no manifest drift",
                "NFR-E08-1",
                "test_every_catalog_attack_is_exercised",
            ),
        ],
    ),
)


class _Collector:
    """Record each test's call-phase outcome, keyed by node id.

    A plain class (not a dataclass) on purpose: a dataclass instance is
    unhashable under pytest's fixture-holder bookkeeping (same note as
    ``compat_check._Collector``)."""

    def __init__(self) -> None:
        self.outcomes: dict[str, str] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when != "call":
            return
        self.outcomes[report.nodeid] = report.outcome  # passed | failed | skipped


def _status_for(case: Case, outcomes: dict[str, str]) -> tuple[str, int, int]:
    """Roll a case's matching node ids into one verdict.

    Returns (mark, passed_count, total_count). A case with no matching node is
    MISSING — a drift between this matrix and the executed suite, treated as a
    failure so the two can never silently diverge (same property the red-team
    manifest test enforces inside the suite)."""
    matched = {nid: out for nid, out in outcomes.items() if f"::{case.node}" in nid}
    total = len(matched)
    passed = sum(1 for out in matched.values() if out == "passed")
    if total == 0:
        return "MISSING", 0, 0
    if any(out == "failed" for out in matched.values()):
        return "FAIL", passed, total
    if all(out == "skipped" for out in matched.values()):
        return "SKIP", passed, total
    return "PASS", passed, total


def _render(date: str, results: list[tuple[Category, list[tuple[Case, str, int, int]]]]) -> str:
    """Render the markdown report body (also the basis for the stdout matrix)."""
    lines: list[str] = []
    lines.append(f"# AI-backend UAT — 試錯 (abuse) matrix · {date}")
    lines.append("")
    lines.append(
        "Adversarial acceptance run against the **real MCP gateway + tools** on a "
        "hermetic SQLite arena (no Supabase, no real workspace). Each row is one "
        "property a compromised or buggy agent must not be able to break; the "
        "`test` column is the reproducing pytest node, the `req` column the "
        "requirement it satisfies. Generated by `scripts/uat_ai_backend.py`."
    )
    lines.append("")
    grand_pass = grand_total = 0
    for cat, rows in results:
        cat_pass = sum(1 for _, mark, _, _ in rows if mark == "PASS")
        lines.append(f"## {cat.key} · {cat.title} — {cat_pass}/{len(rows)} PASS")
        lines.append("")
        lines.append("| UAT | property | expected | result | req | test |")
        lines.append("|---|---|---|---|---|---|")
        for case, mark, passed, total in rows:
            n = f" ({passed}/{total})" if total > 1 else ""
            lines.append(
                f"| {case.uid} | {case.title} | {case.expect} | **{mark}**{n} | "
                f"{case.trace} | `{case.node}` |"
            )
            grand_pass += 1 if mark == "PASS" else 0
            grand_total += 1
        lines.append("")
    verdict = "PASS" if grand_pass == grand_total else "FAIL"
    lines.append(f"**Total: {grand_pass} / {grand_total} {verdict}**")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uat_ai_backend", description=__doc__)
    parser.add_argument(
        "--date",
        default=_dt.date.today().isoformat(),
        help="date to stamp on the matrix (default: today)",
    )
    parser.add_argument(
        "--report", type=Path, default=None, help="also write the full markdown matrix to this path"
    )
    args = parser.parse_args(argv)

    collector = _Collector()
    code = pytest.main(
        ["-q", "-p", "no:cacheprovider", *[str(p) for p in SUITES]],
        plugins=[collector],
    )

    results: list[tuple[Category, list[tuple[Case, str, int, int]]]] = []
    for cat in CATEGORIES:
        rows = [(c, *_status_for(c, collector.outcomes)) for c in cat.cases]
        results.append((cat, rows))

    # Stdout matrix (compact — the report file carries the full table).
    print("\nAI-backend UAT — 試錯 (abuse) matrix (real gateway, hermetic arena):")
    grand_pass = grand_total = 0
    clean = True
    for cat, rows in results:
        cat_pass = sum(1 for _, mark, _, _ in rows if mark == "PASS")
        print(f"\n  {cat.key} {cat.title}: {cat_pass}/{len(rows)}")
        for case, mark, passed, total in rows:
            n = f" ({passed}/{total})" if total > 1 else ""
            print(f"    [{mark:>7}] {case.uid} {case.title}{n}")
            grand_pass += 1 if mark == "PASS" else 0
            grand_total += 1
            clean = clean and mark == "PASS"

    verdict = "PASS" if grand_pass == grand_total else "FAIL"
    print(f"\nAI-backend 試錯: {grand_pass} / {grand_total} {verdict}  ·  {args.date}")
    if not clean:
        print(
            "\nA case did not fully pass (or is MISSING — a drift between this "
            "matrix and the suite). Fix the finding before signing off the UAT."
        )

    if args.report is not None:
        args.report.write_text(_render(args.date, results), encoding="utf-8")
        print(f"\nWrote the full matrix to {args.report}")

    return 0 if code == 0 and clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
