# NOTE: This suite covers Pydantic model shape only, per this repo's existing
# unittest-on-pure-functions convention (no live-DB mocking framework exists
# here). The actual filter effect of wallet_state=locked on real
# driver_profiles.is_suspended rows -- and the real-world impact of the
# auto-suspend-vs-admin-suspend ambiguity documented on this param -- requires
# live Supabase access, which this session/environment does not have, and is
# NOT covered by this file. The filter logic itself is a two-line inline
# `.eq()` branch, not a pure function, so there is nothing meaningful to unit
# test in isolation without inventing a Supabase query-builder stub, which no
# other test file in this repo does either.

import unittest

from app.services.drivers.router import DriverAdminListResponse


class DriverAdminListResponseShapeUnchangedTests(unittest.TestCase):
    def test_response_shape_unchanged_by_wallet_state_addition(self) -> None:
        # BE-8 adds a query param only (wallet_state, filtering by is_suspended)
        # -- confirm the response model itself was not touched by this change
        # (it has exactly `drivers`/`total`, no new fields introduced).
        resp = DriverAdminListResponse(drivers=[], total=0)
        self.assertEqual(resp.drivers, [])
        self.assertEqual(resp.total, 0)
        self.assertEqual(set(DriverAdminListResponse.model_fields.keys()), {"drivers", "total"})


if __name__ == "__main__":
    unittest.main()
