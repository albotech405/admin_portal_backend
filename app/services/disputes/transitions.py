"""Pure, unit-testable dispute status transition rules.

A DB-level CHECK constraint can only validate a status value in isolation --
it cannot express "is X a valid destination FROM the row's CURRENT status,"
which is what every status-changing endpoint needs enforced before it writes.
That logic lives here instead, called by each endpoint before any DB mutation,
raising HTTPException(400, ...) on violation.
"""

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "open":      {"assigned", "escalated", "resolved", "dismissed"},
    "assigned":  {"escalated", "resolved", "dismissed"},
    "escalated": {"resolved", "dismissed"},
    "resolved":  {"reopened"},
    "dismissed": {"reopened"},
    "reopened":  {"assigned", "escalated", "resolved", "dismissed"},
}


def is_valid_transition(from_status: str, to_status: str) -> bool:
    return to_status in _VALID_TRANSITIONS.get(from_status, set())
