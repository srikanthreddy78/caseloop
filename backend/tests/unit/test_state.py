import pytest
from app.cases import state as sm
from app.models import CaseState


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (CaseState.NEW, CaseState.GATHERING),
        (CaseState.GATHERING, CaseState.AWAITING_CUSTOMER),
        (CaseState.AWAITING_CUSTOMER, CaseState.GATHERING),
        (CaseState.GATHERING, CaseState.RESOLVED),
        (CaseState.AWAITING_CUSTOMER, CaseState.ABANDONED),
        (CaseState.RESOLVED, CaseState.GATHERING),
        (CaseState.ESCALATED, CaseState.RESOLVED),
        (CaseState.ESCALATED, CaseState.AWAITING_CUSTOMER),
        (CaseState.ABANDONED, CaseState.GATHERING),
    ],
)
def test_allowed_transitions(current: CaseState, target: CaseState) -> None:
    assert sm.transition(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (CaseState.NEW, CaseState.RESOLVED),
        (CaseState.NEW, CaseState.AWAITING_CUSTOMER),
        (CaseState.RESOLVED, CaseState.ESCALATED),
        (CaseState.ABANDONED, CaseState.RESOLVED),
        (CaseState.GATHERING, CaseState.NEW),
    ],
)
def test_forbidden_transitions(current: CaseState, target: CaseState) -> None:
    with pytest.raises(sm.InvalidTransition):
        sm.transition(current, target)


def test_every_state_has_rules() -> None:
    assert set(sm.ALLOWED) == set(CaseState)


def test_terminal_detection() -> None:
    assert (
        sm.is_terminal("resolved") and sm.is_terminal("escalated") and sm.is_terminal("abandoned")
    )
    assert not sm.is_terminal("gathering")


def test_next_followup_hours_walks_schedule_then_repeats_last() -> None:
    schedule = [24, 72, 168]
    assert [sm.next_followup_hours(schedule, n) for n in range(5)] == [24, 72, 168, 168, 168]
    assert sm.next_followup_hours([], 0) == 24


def test_clamp_never_exceeds_policy_or_drops_below_one_hour() -> None:
    schedule = [24, 72]
    assert sm.clamp_followup_hours(500, schedule, 0) == 24
    assert sm.clamp_followup_hours(6, schedule, 0) == 6
    assert sm.clamp_followup_hours(0, schedule, 1) == 1
    assert sm.clamp_followup_hours(100, schedule, 1) == 72


def test_should_abandon() -> None:
    assert not sm.should_abandon(2, 3)
    assert sm.should_abandon(3, 3)
    assert sm.should_abandon(0, 0)
