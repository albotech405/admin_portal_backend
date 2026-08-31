# NOTE: This suite covers Pydantic model defaults/shape only, per this repo's
# existing unittest-on-pure-functions convention (no live-DB mocking framework
# exists here). Real dispute_logs/rides!inner embed behavior, real pagination
# correctness against live data, the count-query-fails-safely-to-zero behavior,
# and parity between the primary route and its /disputes/admin/list alias under
# real filters all require live Supabase access, which this session/environment
# does not have, and are NOT covered by this file.

import unittest

from app.services.disputes.router import DisputeItem, DisputeListResponse


def _sample_item(**overrides) -> DisputeItem:
    defaults = dict(
        ride_id="r1",
        customer_id="c1",
        status="completed",
        created_at="2026-08-01T00:00:00Z",
    )
    defaults.update(overrides)
    return DisputeItem(**defaults)


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


if __name__ == "__main__":
    unittest.main()
