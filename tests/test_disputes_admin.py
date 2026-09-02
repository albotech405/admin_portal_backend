# NOTE: This suite covers Pydantic model shape/defaults and the pure
# is_valid_transition function only, per this repo's existing
# unittest-on-pure-functions convention (no live-DB mocking framework exists
# here). Real RPC execution (dispute_refund_customer / dispute_charge_driver
# against a live Postgres instance), real concurrent-write transition
# enforcement, real RBAC enforcement (require_role's actual 403 behavior), and
# real notification delivery are NOT covered here and require live Supabase
# access this session/environment does not have.

import unittest

from app.services.disputes.router import DisputeItem, DisputeListResponse, DisputeDetailResponse
from app.services.disputes.transitions import is_valid_transition


def _sample_item(**overrides) -> DisputeItem:
    defaults = dict(
        id="d1",
        filed_by_admin_id="a1",
        filed_for="customer",
        dispute_type="fare",
        description="Customer disputes the fare amount",
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-01T00:00:00Z",
    )
    defaults.update(overrides)
    return DisputeItem(**defaults)


class DisputeItemModelTests(unittest.TestCase):
    def test_defaults(self) -> None:
        item = _sample_item()
        self.assertEqual(item.status, "open")
        self.assertEqual(item.priority, "normal")
        self.assertEqual(item.attachment_urls, [])
        self.assertIsNone(item.ride_id)
        self.assertIsNone(item.customer_id)
        self.assertIsNone(item.driver_id)
        self.assertIsNone(item.resolution_type)

    def test_accepts_real_values(self) -> None:
        item = _sample_item(
            ride_id="r1", customer_id="c1", driver_id="drv1", priority="high",
            status="resolved", resolution_type="refund", resolution_amount_cdf=1000.0,
        )
        self.assertEqual(item.ride_id, "r1")
        self.assertEqual(item.priority, "high")
        self.assertEqual(item.status, "resolved")
        self.assertEqual(item.resolution_type, "refund")
        self.assertEqual(item.resolution_amount_cdf, 1000.0)


class DisputeListResponsePaginationTests(unittest.TestCase):
    def test_limit_and_offset_default_to_unbounded(self) -> None:
        resp = DisputeListResponse(disputes=[], total=0)
        self.assertIsNone(resp.limit)
        self.assertEqual(resp.offset, 0)

    def test_limit_and_offset_round_trip(self) -> None:
        item = _sample_item()
        resp = DisputeListResponse(disputes=[item], total=17, limit=5, offset=10)
        self.assertEqual(resp.total, 17)
        self.assertEqual(resp.limit, 5)
        self.assertEqual(resp.offset, 10)
        self.assertEqual(len(resp.disputes), 1)

    def test_total_is_independent_of_page_size(self) -> None:
        resp = DisputeListResponse(disputes=[_sample_item()], total=200, limit=1, offset=0)
        self.assertEqual(resp.total, 200)
        self.assertEqual(len(resp.disputes), 1)


class DisputeDetailResponseTests(unittest.TestCase):
    def test_history_defaults_to_empty_list(self) -> None:
        detail = DisputeDetailResponse(
            id="d1", filed_by_admin_id="a1", filed_for="driver", dispute_type="safety",
            description="x", created_at="t", updated_at="t",
        )
        self.assertEqual(detail.history, [])


class TransitionTests(unittest.TestCase):
    VALID = [
        ("open", "assigned"), ("open", "escalated"), ("open", "resolved"), ("open", "dismissed"),
        ("assigned", "escalated"), ("assigned", "resolved"), ("assigned", "dismissed"),
        ("escalated", "resolved"), ("escalated", "dismissed"),
        ("resolved", "reopened"), ("dismissed", "reopened"),
        ("reopened", "assigned"), ("reopened", "escalated"), ("reopened", "resolved"), ("reopened", "dismissed"),
    ]
    INVALID = [
        ("open", "reopened"), ("resolved", "assigned"), ("resolved", "escalated"),
        ("dismissed", "assigned"), ("dismissed", "resolved"), ("assigned", "open"),
        ("escalated", "open"), ("escalated", "assigned"), ("open", "open"),
        ("unknown_status", "open"),
    ]

    def test_valid_transitions(self) -> None:
        for f, t in self.VALID:
            with self.subTest(f=f, t=t):
                self.assertTrue(is_valid_transition(f, t))

    def test_invalid_transitions(self) -> None:
        for f, t in self.INVALID:
            with self.subTest(f=f, t=t):
                self.assertFalse(is_valid_transition(f, t))


if __name__ == "__main__":
    unittest.main()
