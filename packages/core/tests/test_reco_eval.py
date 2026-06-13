"""Light recommendation eval (MOD-22 / E17-T3, FR-E17-3).

The 30 hand-graded fixtures are the regression guard on the role recommendations:
the confusion matrix must stay perfect (no false positives or negatives) because
the engine is deterministic — a drift in a container's role or a signal rule
would show up here as FP/FN.
"""

from __future__ import annotations

from kantaq_core import memory_policy, reco


def test_reco_fixture_set_has_thirty_cases() -> None:
    fixtures = reco.load_reco_fixtures()
    assert len(fixtures) == 30
    assert all(fx.expected_roles for fx in fixtures)


def test_reco_confusion_matrix_is_perfect() -> None:
    """recommend() agrees with the hand-graded expectations on every cell."""
    fixtures = reco.load_reco_fixtures()
    matrix = reco.confusion_matrix(fixtures)
    print("\n" + matrix.render())  # surfaces the matrix in test output (E17-T3 deliverable)

    assert matrix.fixtures == 30
    cells = (
        matrix.true_positives
        + matrix.false_positives
        + matrix.false_negatives
        + matrix.true_negatives
    )
    assert cells == 30 * len(memory_policy.ROLE_SLUGS) == 120
    assert matrix.false_positives == 0
    assert matrix.false_negatives == 0
    assert matrix.precision == 1.0
    assert matrix.recall == 1.0
    assert matrix.accuracy == 1.0
    # The matrix is non-trivial: real positives and real negatives both occur.
    assert matrix.true_positives > 0
    assert matrix.true_negatives > 0


def test_confusion_matrix_catches_a_regressed_expectation() -> None:
    """A fixture whose expectation no longer matches the engine shows as FN/FP."""
    # Pretend a grader expected a role the implementation-stage engine won't surface.
    broken = reco.RecoFixture(
        id="BROKEN",
        title="t",
        lifecycle_stage="implementation",
        labels=(),
        expected_roles=frozenset({"design_agent"}),  # implementation -> code_agent only
    )
    matrix = reco.confusion_matrix([broken])
    assert matrix.false_negatives == 1  # expected design_agent, not recommended
    assert matrix.false_positives == 1  # recommended code_agent, not expected
    assert matrix.precision < 1.0
