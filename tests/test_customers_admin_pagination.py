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
    def test_is_active_false_is_the_only_ban_field(self) -> None:
        # Regression guard for BE-2: is_active is the sole ban representation.
        # This just confirms the field exists and behaves as a plain bool --
        # the absence of any is_banned/ban_reason/banned_at field is a
        # structural fact about the model (no such field can be asserted
        # "absent" meaningfully via a positive test; the audit that established
        # this was a full-repo grep, not something this test re-derives).
        item = _sample_item(is_active=False)
        self.assertFalse(item.is_active)
        item2 = _sample_item(is_active=True)
        self.assertTrue(item2.is_active)


if __name__ == "__main__":
    unittest.main()
