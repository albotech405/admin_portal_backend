import unittest

from app.services.customer_wallet.router import (
    ChangeCreditItem,
    CustomerWalletTransactionItem,
    DriverReferralItem,
    ReferralItem,
)


class CustomerWalletAdminModelTests(unittest.TestCase):
    def test_customer_wallet_transaction_item_distinguishes_reward_sources(self) -> None:
        first_ride = CustomerWalletTransactionItem(
            id="t1", user_id="u1", type="credit", amount_cdf=1000, source="first_ride",
            created_at="2026-08-01T00:00:00Z",
        )
        change = CustomerWalletTransactionItem(
            id="t2", user_id="u1", type="credit", amount_cdf=4500, source="change",
            ride_id="r1", created_at="2026-08-01T00:00:00Z",
        )
        self.assertEqual(first_ride.source, "first_ride")
        self.assertEqual(change.source, "change")
        self.assertEqual(change.ride_id, "r1")
        self.assertIsNone(first_ride.ride_id)

    def test_change_credit_item_reports_5000_cap_context(self) -> None:
        item = ChangeCreditItem(
            id="t2", ride_id="r1", customer_id="u1", driver_id="d1",
            amount_cdf=5000, created_at="2026-08-01T00:00:00Z",
        )
        self.assertEqual(item.amount_cdf, 5000)
        self.assertEqual(item.driver_id, "d1")

    def test_referral_item_keeps_referrer_and_referred_rewards_distinct(self) -> None:
        item = ReferralItem(
            id="ref1", referrer_user_id="u1", referred_user_id="u2",
            referrer_reward_cdf=4000, referred_reward_cdf=2000,
            status="completed", reward_given=True,
        )
        self.assertNotEqual(item.referrer_reward_cdf, item.referred_reward_cdf)
        self.assertEqual(item.referrer_reward_cdf, 4000)
        self.assertEqual(item.referred_reward_cdf, 2000)

    def test_driver_referral_item_resolves_name_from_direct_user_embed(self) -> None:
        # referred_driver_id is nullable on driver_referrals (a referral row can exist
        # before the referred person's driver profile is created), so name/phone must
        # come from referrer_user_id/referred_user_id (both NOT NULL), not the driver FK.
        item = DriverReferralItem(
            id="dref1", referrer_driver_id="d1", referred_driver_id=None,
            referred_name="Amina K.", referred_phone="+243812345678",
        )
        self.assertIsNone(item.referred_driver_id)
        self.assertEqual(item.referred_name, "Amina K.")
        self.assertEqual(item.referred_phone, "+243812345678")

    def test_driver_referral_item_uses_10_ride_trigger_amounts(self) -> None:
        item = DriverReferralItem(
            id="dref1", referrer_driver_id="d1", referred_driver_id="d2",
            referrer_reward_cdf=10000, referred_reward_cdf=5000,
            completed_rides=10, required_rides=10, status="completed",
        )
        self.assertEqual(item.referrer_reward_cdf, 10000)
        self.assertEqual(item.referred_reward_cdf, 5000)
        self.assertEqual(item.completed_rides, item.required_rides)


if __name__ == "__main__":
    unittest.main()
