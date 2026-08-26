# NOTE: This suite covers pure functions and Pydantic model defaults only, per
# this repo's existing unittest-on-pure-functions convention (no live-DB mocking
# framework exists here). Actual row lookups against service_areas (real
# active/inactive filtering behavior, real 404-on-missing-id behavior, real N+1
# absence under real data, actual bbox values returned) require live Supabase
# access, which this session/environment does not have, and are NOT exercised
# by this file.

import unittest

from app.services.config.router import ServiceAreaItem, ServiceAreasResponse
from app.services.analytics.router import _bbox_from_service_area_row


class ServiceAreaItemModelTests(unittest.TestCase):
    def test_service_area_item_holds_hierarchy_and_bbox_fields(self) -> None:
        item = ServiceAreaItem(
            id="11111111-1111-1111-1111-111111111111",
            country_code="CD",
            country_name="Democratic Republic of the Congo",
            city="Kinshasa",
            area_name="Kinshasa Metro",
            is_active=True,
            north=-4.20, south=-4.50, east=15.40, west=15.15,
        )
        self.assertEqual(item.country_code, "CD")
        self.assertEqual(item.city, "Kinshasa")
        self.assertEqual(item.area_name, "Kinshasa Metro")
        self.assertTrue(item.is_active)
        self.assertEqual((item.north, item.south, item.east, item.west), (-4.20, -4.50, 15.40, 15.15))

    def test_service_area_item_is_active_defaults_to_true(self) -> None:
        item = ServiceAreaItem(
            id="x", country_code="CD", country_name="DRC", city="Kinshasa", area_name="Kinshasa Metro",
            north=-4.20, south=-4.50, east=15.40, west=15.15,
        )
        self.assertTrue(item.is_active)


class ServiceAreasResponseModelTests(unittest.TestCase):
    def test_empty_areas_list_by_default(self) -> None:
        resp = ServiceAreasResponse()
        self.assertEqual(resp.areas, [])

    def test_holds_multiple_area_items(self) -> None:
        item = ServiceAreaItem(
            id="x", country_code="CD", country_name="DRC", city="Kinshasa", area_name="Kinshasa Metro",
            north=-4.20, south=-4.50, east=15.40, west=15.15,
        )
        resp = ServiceAreasResponse(areas=[item])
        self.assertEqual(len(resp.areas), 1)
        self.assertEqual(resp.areas[0].city, "Kinshasa")


class BboxFromServiceAreaRowTests(unittest.TestCase):
    def test_extracts_bbox_fields_from_row_dict(self) -> None:
        row = {
            "id": "x", "country_code": "CD", "city": "Kinshasa",
            "north": -4.20, "south": -4.50, "east": 15.40, "west": 15.15,
        }
        self.assertEqual(
            _bbox_from_service_area_row(row),
            {"north": -4.20, "south": -4.50, "east": 15.40, "west": 15.15},
        )

    def test_ignores_extra_columns_on_the_row(self) -> None:
        row = {
            "north": 1.0, "south": 0.0, "east": 2.0, "west": -1.0,
            "is_active": True, "area_name": "Somewhere",
        }
        self.assertEqual(_bbox_from_service_area_row(row), {"north": 1.0, "south": 0.0, "east": 2.0, "west": -1.0})

    def test_coerces_numeric_strings_to_float(self) -> None:
        # PostgREST can serialize double precision columns as strings in some
        # client configurations; the helper must not assume a specific numeric
        # wire type.
        row = {"north": "1.0", "south": "0.0", "east": "2.0", "west": "-1.0"}
        self.assertEqual(_bbox_from_service_area_row(row), {"north": 1.0, "south": 0.0, "east": 2.0, "west": -1.0})


if __name__ == "__main__":
    unittest.main()
