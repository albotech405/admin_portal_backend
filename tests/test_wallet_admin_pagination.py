# NOTE: This suite covers Pydantic model defaults/shape only, per this repo's
# existing unittest-on-pure-functions convention (no live-DB mocking framework
# exists here). Real wallet_topup_requests/wallet_transactions row counts, embed
# correctness (driver_profiles(user_id, users(...))), and the RPC-driven
# approve/reject paths (untouched by this batch) all require live Supabase
# access, which this session/environment does not have, and are NOT covered by
# this file.

import unittest

from app.services.wallet.router import (
    TopupRequest,
    TopupRequestsResponse,
    WalletTransaction,
    WalletTransactionListResponse,
)


def _sample_topup(**overrides) -> TopupRequest:
    defaults = dict(
        id="t1",
        driver_id="d1",
        amount=5000.0,
        status="pending",
        submitted_at="2026-08-01T00:00:00Z",
    )
    defaults.update(overrides)
    return TopupRequest(**defaults)


def _sample_transaction(**overrides) -> WalletTransaction:
    defaults = dict(
        id="w1",
        driver_id="d1",
        type="credit",
        amount=1000.0,
        balance_after=6000.0,
        created_at="2026-08-01T00:00:00Z",
    )
    defaults.update(overrides)
    return WalletTransaction(**defaults)


class TopupRequestsResponsePaginationTests(unittest.TestCase):
    def test_limit_and_offset_default_to_unbounded(self) -> None:
        resp = TopupRequestsResponse(requests=[], total=0)
        self.assertIsNone(resp.limit)
        self.assertEqual(resp.offset, 0)

    def test_limit_and_offset_round_trip(self) -> None:
        resp = TopupRequestsResponse(requests=[_sample_topup()], total=30, limit=10, offset=10)
        self.assertEqual(resp.total, 30)
        self.assertEqual(resp.limit, 10)
        self.assertEqual(resp.offset, 10)


class WalletTransactionListResponseTotalRegressionTests(unittest.TestCase):
    def test_total_field_now_exists_and_defaults_to_zero(self) -> None:
        # Regression guard for BE-6: this response previously had NO total field
        # at all -- confirm it now exists with a safe default.
        resp = WalletTransactionListResponse(transactions=[])
        self.assertEqual(resp.total, 0)
        self.assertIsNone(resp.limit)
        self.assertEqual(resp.offset, 0)

    def test_limit_and_offset_round_trip(self) -> None:
        txn = _sample_transaction()
        resp = WalletTransactionListResponse(transactions=[txn], total=99, limit=20, offset=40)
        self.assertEqual(resp.total, 99)
        self.assertEqual(resp.limit, 20)
        self.assertEqual(resp.offset, 40)

    def test_reference_fields_preserved(self) -> None:
        # BE-6 explicitly requires reference_id/reference_type to remain intact.
        txn = _sample_transaction(reference_id="ride-123", reference_type="ride_commission")
        self.assertEqual(txn.reference_id, "ride-123")
        self.assertEqual(txn.reference_type, "ride_commission")


if __name__ == "__main__":
    unittest.main()
