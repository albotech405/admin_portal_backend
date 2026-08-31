# NOTE: This suite covers Pydantic model shape only, per this repo's existing
# unittest-on-pure-functions convention (no live-DB mocking framework exists
# here). Whether real `rides` rows returned by live Supabase actually populate
# customer_name/driver_name/distance_km/category (vs. being NULL for old rows,
# or reflecting live schema drift) requires live DB access, which this
# session/environment does not have, and is NOT covered by this file. The
# audit backing this change established that OTHER endpoints (RideDetailResponse,
# live_location) already read these columns successfully off an unjoined
# select("*") -- not that every row has non-null values.

import unittest

from app.services.rides.router import RideResponse, RideDetailResponse, _RIDE_FIELDS


def _sample_ride(**overrides) -> dict:
    defaults = dict(
        id="r1",
        customer_id="c1",
        status="completed",
        created_at="2026-08-01T00:00:00Z",
    )
    defaults.update(overrides)
    return defaults


class RideResponseIdentityFieldsTests(unittest.TestCase):
    def test_new_fields_default_to_none_when_omitted(self) -> None:
        ride = RideResponse(**_sample_ride())
        self.assertIsNone(ride.customer_name)
        self.assertIsNone(ride.driver_name)
        self.assertIsNone(ride.distance_km)
        self.assertIsNone(ride.category)

    def test_new_fields_accept_real_values(self) -> None:
        ride = RideResponse(**_sample_ride(
            customer_name="Jane Doe",
            driver_name="John Smith",
            distance_km=12.4,
            category="premium",
        ))
        self.assertEqual(ride.customer_name, "Jane Doe")
        self.assertEqual(ride.driver_name, "John Smith")
        self.assertEqual(ride.distance_km, 12.4)
        self.assertEqual(ride.category, "premium")

    def test_ride_fields_constant_includes_new_fields(self) -> None:
        # Direct regression guard: _RIDE_FIELDS (used to filter raw Supabase
        # rows before constructing RideResponse) is derived from
        # RideResponse.model_fields.keys() -- confirm it picked up all four
        # new fields, since a future refactor that hardcodes this set again
        # would silently reintroduce the original bug.
        for field in ("customer_name", "driver_name", "distance_km", "category"):
            self.assertIn(field, _RIDE_FIELDS)

    def test_ride_detail_response_inherits_identity_fields(self) -> None:
        # RideDetailResponse no longer redeclares these fields -- confirm they
        # still work via inheritance from RideResponse.
        detail = RideDetailResponse(**_sample_ride(
            customer_name="Jane Doe",
            driver_name="John Smith",
            distance_km=8.1,
            category="standard",
        ))
        self.assertEqual(detail.customer_name, "Jane Doe")
        self.assertEqual(detail.driver_name, "John Smith")
        self.assertEqual(detail.distance_km, 8.1)
        self.assertEqual(detail.category, "standard")


if __name__ == "__main__":
    unittest.main()
