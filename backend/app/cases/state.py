"""Case state machine and follow-up cadence policy. Pure functions, no I/O."""

from __future__ import annotations

from app.models import TERMINAL_STATES, CaseState

ALLOWED: dict[CaseState, frozenset[CaseState]] = {
    CaseState.NEW: frozenset({CaseState.GATHERING, CaseState.ESCALATED}),
    CaseState.GATHERING: frozenset(
        {CaseState.AWAITING_CUSTOMER, CaseState.RESOLVED, CaseState.ESCALATED, CaseState.ABANDONED}
    ),
    CaseState.AWAITING_CUSTOMER: frozenset(
        {CaseState.GATHERING, CaseState.RESOLVED, CaseState.ESCALATED, CaseState.ABANDONED}
    ),
    # Terminal states can only be reopened (or, for escalated, closed by a human).
    CaseState.RESOLVED: frozenset({CaseState.GATHERING}),
    CaseState.ESCALATED: frozenset({CaseState.GATHERING, CaseState.RESOLVED}),
    CaseState.ABANDONED: frozenset({CaseState.GATHERING}),
}


class InvalidTransition(ValueError):
    pass


def transition(current: str, target: str) -> CaseState:
    cur, tgt = CaseState(current), CaseState(target)
    if tgt not in ALLOWED[cur]:
        raise InvalidTransition(f"cannot move case from {cur} to {tgt}")
    return tgt


def is_terminal(state: str) -> bool:
    return CaseState(state) in TERMINAL_STATES


def next_followup_hours(schedule: list[int], followups_sent: int) -> int:
    """Hours to wait before the next nudge. Past the end of the schedule, the last entry repeats."""
    if not schedule:
        return 24
    return int(schedule[min(followups_sent, len(schedule) - 1)])


def clamp_followup_hours(requested: int, schedule: list[int], followups_sent: int) -> int:
    """The agent may ask sooner than policy but never later; never under one hour."""
    ceiling = next_followup_hours(schedule, followups_sent)
    return max(1, min(int(requested), ceiling))


def should_abandon(followups_sent: int, max_followups: int) -> bool:
    return followups_sent >= max_followups
