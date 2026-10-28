"""Unit tests for the state machine, independent of the database and HTTP."""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.state import (
    ACTIONS,
    TRANSITIONS,
    InvalidTransition,
    OrderState,
    assert_transition,
    can_transition,
    is_terminal,
)

HAPPY_PATH = [
    OrderState.CREATED,
    OrderState.ACCEPTED,
    OrderState.ASSIGNED,
    OrderState.PICKED_UP,
    OrderState.DELIVERED,
]


def test_happy_path_is_legal_step_by_step() -> None:
    for current, target in pairwise(HAPPY_PATH):
        assert can_transition(current, target)
        assert_transition(current, target)


@pytest.mark.parametrize("state", list(OrderState))
def test_cancel_is_allowed_only_from_created_or_accepted(state: OrderState) -> None:
    expected = state in (OrderState.CREATED, OrderState.ACCEPTED)
    assert can_transition(state, OrderState.CANCELED) is expected


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert is_terminal(OrderState.DELIVERED) and is_terminal(OrderState.CANCELED)
    assert not any(is_terminal(state) for state in HAPPY_PATH[:-1])


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderState.CREATED, OrderState.ASSIGNED),
        (OrderState.CREATED, OrderState.DELIVERED),
        (OrderState.ACCEPTED, OrderState.PICKED_UP),
        (OrderState.ASSIGNED, OrderState.CANCELED),
        (OrderState.DELIVERED, OrderState.CREATED),
        (OrderState.CANCELED, OrderState.ACCEPTED),
    ],
)
def test_skipping_or_reversing_steps_raises(current: OrderState, target: OrderState) -> None:
    with pytest.raises(InvalidTransition) as info:
        assert_transition(current, target)
    assert current.value in str(info.value) and target.value in str(info.value)


def test_every_state_is_in_the_table_and_every_action_targets_a_non_initial_state() -> None:
    assert set(TRANSITIONS) == set(OrderState)
    assert set(ACTIONS.values()) == set(OrderState) - {OrderState.CREATED}
