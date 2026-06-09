"""Builders: sane defaults, overrides applied, deterministic ids per seed."""

from kantaq_test_harness import SeededRandom, build_ticket, build_workspace


def test_builder_defaults_and_overrides() -> None:
    ticket = build_ticket(SeededRandom(0), title="Login bug", status="in_progress")
    assert ticket.title == "Login bug"
    assert ticket.status == "in_progress"
    assert ticket.id.startswith("tkt_")
    assert ticket.priority == "medium"  # untouched default


def test_builder_ids_are_deterministic_per_seed() -> None:
    assert build_workspace(SeededRandom(5)).id == build_workspace(SeededRandom(5)).id
