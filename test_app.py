import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb


class OwnedStampCountsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = duckdb.connect(":memory:")
        spec = importlib.util.spec_from_file_location("app_under_test", Path(__file__).with_name("app.py"))
        cls.app = importlib.util.module_from_spec(spec)
        with patch("duckdb.connect", return_value=cls.con):
            spec.loader.exec_module(cls.app)
        cls.app.initialize_stamps_table()

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def setUp(self):
        self.con.execute("DELETE FROM stamps")

    def test_counts_all_configured_regions_and_only_owned_stamps(self):
        self.con.execute(
            "INSERT INTO stamps (gebiet, katalognummer, besitz) VALUES "
            "('USA', 1, 1), ('USA', 2, 0), ('Südafrika', 3, 2), ('Other', 4, 1)"
        )
        expected = dict.fromkeys(self.app.GEBIETE, 0)
        expected.update({"USA": 1, "Südafrika": 1})
        self.assertEqual(self.app.get_owned_stamp_counts(), expected)

    def test_empty_configured_regions(self):
        with patch.object(self.app, "GEBIETE", []):
            self.assertEqual(self.app.get_owned_stamp_counts(), {})

    def test_missing_stamps_filter_by_region_and_zero_possession(self):
        self.con.execute(
            "INSERT INTO stamps (gebiet, katalognummer, besitz) VALUES "
            "('USA', 20, 0), ('USA', 10, 0), ('USA', 30, 1), "
            "('USA', 40, NULL), ('AD Baden', 5, 0)"
        )
        all_missing = self.app.get_missing_stamps(self.app.ALL_GEBIETE)
        self.assertEqual([row["katalognummer"] for row in all_missing], [5, 10, 20])
        usa_missing = self.app.get_missing_stamps("USA")
        self.assertEqual([row["katalognummer"] for row in usa_missing], [10, 20])
        self.assertEqual(self.app.get_missing_stamps("Südafrika"), [])


if __name__ == "__main__":
    unittest.main()
