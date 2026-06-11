import unittest

from app.services.live_location.router import _build_sos_session


class SosLiveLocationBuilderTests(unittest.TestCase):
    def test_build_sos_session_uses_sos_fallback_when_realtime_rows_missing(self) -> None:
        session = _build_sos_session(
            {
                "id": "308b680d-79b8-4ca4-9330-32e49693c345",
                "user_id": "user-1",
                "triggered_at": "2026-06-09T08:29:00Z",
                "last_location_update": "2026-06-09T08:30:05.212168Z",
                "latitude": -4.4084873,
                "longitude": 15.2537206,
                "triggered_by_driver": False,
                "is_active": True,
                "ride_id": None,
            },
            linked_ride=None,
            ride_locations={},
            driver_profiles_by_id={},
            driver_profiles_by_user_id={},
            users_by_id={
                "user-1": {
                    "id": "user-1",
                    "full_name": "Kin",
                    "phone_number": "+243988931792",
                }
            },
            route_path=[],
        )

        self.assertEqual(session.customer_name, "Kin")
        self.assertEqual(session.customer_phone, "+243988931792")
        self.assertEqual(session.last_location_timestamp, "2026-06-09T08:30:05.212168Z")
        self.assertFalse(session.waiting_for_first_update)
        self.assertEqual(len(session.route_path), 1)
        self.assertEqual(session.route_path[0].latitude, -4.4084873)
        self.assertEqual(session.route_path[0].longitude, 15.2537206)
        self.assertIsNotNone(session.participants.customer)
        self.assertIsNotNone(session.participants.customer.point)
        self.assertEqual(session.participants.customer.point.latitude, -4.4084873)
        self.assertEqual(session.participants.customer.point.longitude, 15.2537206)


if __name__ == "__main__":
    unittest.main()