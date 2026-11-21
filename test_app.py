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


class SpecialStampsTableTests(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        spec = importlib.util.spec_from_file_location("app_under_test", Path(__file__).with_name("app.py"))
        self.app = importlib.util.module_from_spec(spec)
        with patch("duckdb.connect", return_value=self.con):
            spec.loader.exec_module(self.app)
        self.app.initialize_stamps_table()

    def tearDown(self):
        self.con.close()

    def test_schema_matches_general_stamps_with_string_type(self):
        self.app.initialize_special_stamps_table()
        general = self.con.execute("DESCRIBE stamps").fetchall()
        special = self.con.execute("DESCRIBE special_stamps").fetchall()
        # ID defaults refer to different sequences; all other properties match.
        self.assertEqual([row[:4] for row in special[:-1]], [row[:4] for row in general])
        self.assertEqual(special[-1][:2], ("typ", "VARCHAR"))

    def test_initialization_is_repeatable_and_keeps_collections_separate(self):
        self.con.execute("INSERT INTO stamps (katalognummer) VALUES (10)")
        self.app.initialize_special_stamps_table()
        self.con.execute(
            "INSERT INTO special_stamps (katalognummer, typ) VALUES (20, 'Block')"
        )
        self.app.initialize_special_stamps_table()
        self.con.execute(
            "INSERT INTO special_stamps (katalognummer, typ) VALUES (21, 'Bogen')"
        )
        self.assertEqual(self.con.execute("SELECT id, katalognummer FROM stamps").fetchall(), [(1, 10)])
        self.assertEqual(
            self.con.execute("SELECT id, katalognummer, typ FROM special_stamps ORDER BY id").fetchall(),
            [(1, 20, "Block"), (2, 21, "Bogen")],
        )

    def test_special_collection_crud_and_views_are_isolated(self):
        self.app.initialize_special_stamps_table()
        special_gebiet = self.app.SPEZIAL_GEBIETE[0]
        general_id = self.app.add_stamp("USA", 10, "gestempelt", 1, "")
        special_id = self.app.add_stamp(special_gebiet, 10, "postfrisch", 0, "", special=True, typ="Block")
        self.assertEqual(general_id, special_id)
        self.assertEqual(self.app.get_gebiete(), ["USA"])
        self.assertEqual(self.app.get_gebiete(special=True), [special_gebiet])
        self.assertEqual(self.app.get_owned_stamp_counts()["USA"], 1)
        self.assertEqual(self.app.get_owned_stamp_counts(special=True), {special_gebiet: 0})
        self.assertEqual(self.app.get_stamps_by_gebiet(special_gebiet, "10", special=True)[0]["typ"], "Block")
        self.assertEqual(self.app.get_stamps_by_gebiet("USA", special=True), [])
        self.assertEqual(self.app.get_missing_stamps(special_gebiet, special=True)[0]["typ"], "Block")
        self.assertEqual(self.app.get_missing_stamps(self.app.ALL_GEBIETE), [])
        self.assertEqual(self.app.get_collection_stats(special_gebiet, special=True)[0]["gesamt"], 1)

        self.assertTrue(self.app.update_stamp(special_id, besitz=1, typ="Bogen", special=True))
        self.assertEqual(self.app.get_owned_stamp_counts(special=True), {special_gebiet: 1})
        self.assertEqual(self.app.get_missing_stamps(special_gebiet, special=True), [])
        self.assertEqual(self.app.get_collection_stats(special_gebiet, special=True)[0]["besitz"], 1)
        self.assertEqual(self.app.get_stamps_by_gebiet(special_gebiet, special=True)[0]["typ"], "Bogen")
        self.assertTrue(self.app.delete_stamp(special_id, special=True))
        self.assertEqual(self.app.get_stamps_by_gebiet(special_gebiet, special=True), [])
        self.assertEqual(self.app.get_stamps_by_gebiet("USA")[0]["id"], general_id)

    def test_collection_forms_use_and_validate_their_configured_regions(self):
        general_form = self.app.stamp_form()
        special_form = self.app.stamp_form(special=True)
        self.assertEqual(general_form["gebiet"].options, self.app.GEBIETE)
        self.assertEqual(special_form["gebiet"].options, self.app.SPEZIAL_GEBIETE)
        special_form["gebiet"].value = self.app.SPEZIAL_GEBIETE[0]
        special_form["katalognummer"].value = 1
        self.assertIsNotNone(self.app.read_form(special_form, special=True))
        with patch.object(self.app.ui, "notify") as notify:
            self.assertIsNone(self.app.read_form(special_form))
            notify.assert_called_once()
        special_form["gebiet"].set_options(self.app.SPEZIAL_GEBIETE + ["USA"], value="USA")
        self.assertIsNotNone(self.app.read_form(special_form, special=True, existing_gebiet="USA"))
        with patch.object(self.app.ui, "notify"):
            self.assertIsNone(self.app.read_form(special_form, special=True))

    def test_empty_special_regions_produce_no_counts(self):
        with patch.object(self.app, "SPEZIAL_GEBIETE", []):
            self.assertEqual(self.app.get_owned_stamp_counts(special=True), {})

    def test_special_range_stores_type_and_skips_existing_special_numbers(self):
        self.app.initialize_special_stamps_table()
        self.app.add_stamp("USA", 10, "", 1, "")
        self.app.add_stamp("USA", 11, "", 0, "", special=True, typ="Alt")
        inserted = self.app.add_stamp_range("USA", 10, 12, "", 0, "", special=True, typ="Block")
        self.assertEqual(inserted, 2)
        rows = self.app.get_stamps_by_gebiet("USA", special=True)
        self.assertEqual([(row["katalognummer"], row["typ"]) for row in rows],
                         [(10, "Block"), (11, "Alt"), (12, "Block")])
        self.assertEqual(len(self.app.get_stamps_by_gebiet("USA")), 1)


if __name__ == "__main__":
    unittest.main()
