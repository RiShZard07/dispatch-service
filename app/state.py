"""The order state machine: the only place that knows which transitions are legal."""

from __future__ import annotations

from enum import StrEnum


class OrderState(StrEnum):
    """Lifecycle states of an order."""

    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    ASSIGNED = "ASSIGNED"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"
    CANCELED = "CANCELED"


# CREATED -> ACCEPTED -> ASSIGNED -> PICKED_UP -> DELIVERED, with CANCELED
# reachable only from CREATED or ACCEPTED. DELIVERED and CANCELED are terminal.
TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.ACCEPTED, OrderState.CANCELED}),
    OrderState.ACCEPTED: frozenset({OrderState.ASSIGNED, OrderState.CANCELED}),
    OrderState.ASSIGNED: frozenset({OrderState.PICKED_UP}),
    OrderState.PICKED_UP: frozenset({OrderState.DELIVERED}),
    OrderState.DELIVERED: frozenset(),
    OrderState.CANCELED: frozenset(),
}

# API action name -> state it moves the order into.
ACTIONS: dict[str, OrderState] = {
    "accept": OrderState.ACCEPTED,
    "assign": OrderState.ASSIGNED,
    "pickup": OrderState.PICKED_UP,
    "deliver": OrderState.DELIVERED,
    "cancel": OrderState.CANCELED,
}


class InvalidTransition(Exception):
    """Raised when an order is asked to move to a state it cannot reach from its current one."""

    def __init__(self, current: OrderState, target: OrderState) -> None:
        self.current, self.target = current, target
        super().__init__(f"cannot move an order from {current.value} to {target.value}")


def can_transition(current: OrderState, target: OrderState) -> bool:
    """Return True if the move from current to target is allowed."""
    return target in TRANSITIONS[current]


def assert_transition(current: OrderState, target: OrderState) -> None:
    """Raise InvalidTransition unless the move is allowed."""
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


def is_terminal(state: OrderState) -> bool:
    """Return True if no transition leaves the given state."""
    return not TRANSITIONS[state]
