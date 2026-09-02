# NOTE: This suite covers Pydantic model defaults/shape only, per this repo's
# existing unittest-on-pure-functions convention (no live-DB mocking framework
# exists here). Real pagination correctness (.range() page boundaries, count="exact"
# accuracy under concurrent writes, search+status combined with limit/offset)
# against a real `users` table requires live Supabase access, which this
# session/environment does not have, and is NOT covered by this file.

import unittest

from app.services.customers.router import CustomerAdminItem, CustomerAdminListResponse


def _sample_item(**overrides) -> CustomerAdminItem:
    defaults = dict(
        id="u1",
        full_name="Jane Doe",
        phone_number="+243900000000",
        is_active=True,
        created_at="2026-08-01T00:00:00Z",
        updated_at="2026-08-01T00:00:00Z",
    )
    defaults.update(overrides)
    return CustomerAdminItem(**defaults)


class CustomerAdminListResponsePaginationTests(unittest.TestCase):
    def test_limit_and_offset_default_to_unbounded(self) -> None:
        resp = CustomerAdminListResponse(customers=[], total=0)
        self.assertIsNone(resp.limit)
        self.assertEqual(resp.offset, 0)

    def test_limit_and_offset_round_trip(self) -> None:
        item = _sample_item()
        resp = CustomerAdminListResponse(customers=[item], total=42, limit=10, offset=20)
        self.assertEqual(resp.total, 42)
        self.assertEqual(resp.limit, 10)
        self.assertEqual(resp.offset, 20)
        self.assertEqual(len(resp.customers), 1)

    def test_total_is_independent_of_page_size(self) -> None:
        # total reports the full match count, not len(customers) -- a page of 1
        # item can still report a much larger total.
        resp = CustomerAdminListResponse(customers=[_sample_item()], total=500, limit=1, offset=0)
        self.assertEqual(resp.total, 500)
        self.assertEqual(len(resp.customers), 1)


class CustomerAdminItemBanRepresentationTests(unittest.TestCase):
    def test_is_active_is_the_sole_ban_state(self) -> None:
        # is_active alone determines banned/active status -- ban_reason/
        # banned_at/banned_by (added below) are contextual metadata about a
        # ban, not an alternate representation of ban state itself.
        item = _sample_item(is_active=False)
        self.assertFalse(item.is_active)
        item2 = _sample_item(is_active=True)
        self.assertTrue(item2.is_active)

    def test_ban_context_fields_default_to_none(self) -> None:
        item = _sample_item()
        self.assertIsNone(item.ban_reason)
        self.assertIsNone(item.banned_at)
        self.assertIsNone(item.banned_by)
        self.assertIsNone(item.banned_by_admin_name)

    def test_ban_context_fields_accept_real_values(self) -> None:
        item = _sample_item(
            is_active=False,
            ban_reason="Fraudulent activity",
            banned_at="2026-09-01T12:00:00Z",
            banned_by="admin-uuid-1",
            banned_by_admin_name="Alice Admin",
        )
        self.assertFalse(item.is_active)
        self.assertEqual(item.ban_reason, "Fraudulent activity")
        self.assertEqual(item.banned_at, "2026-09-01T12:00:00Z")
        self.assertEqual(item.banned_by, "admin-uuid-1")
        self.assertEqual(item.banned_by_admin_name, "Alice Admin")

    def test_unban_clears_ban_context_to_none(self) -> None:
        # Mirrors unban_customer's behavior: is_active=true with all three
        # context fields cleared back to null.
        item = _sample_item(is_active=True, ban_reason=None, banned_at=None, banned_by=None)
        self.assertTrue(item.is_active)
        self.assertIsNone(item.ban_reason)
        self.assertIsNone(item.banned_at)
        self.assertIsNone(item.banned_by)


if __name__ == "__main__":
    unittest.main()
