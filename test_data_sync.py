"""Tests for local snapshot parsing and the read-only public API helpers."""
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import local_index
import portal
import sync_data


class SnapshotParserTest(unittest.TestCase):
    def test_ksr_cp1251_csv(self):
        raw = (
            "Код ОКПД2,Код КСР,Наименование,Единица измерения,\r\n"
            ",01.2-0001,Песок строительный,м3,\r\n"
            ",91.01.01-014,Бульдозер,маш.-ч,\r\n"
        ).encode("cp1251")
        with tempfile.TemporaryDirectory() as tmp:
            db = sync_data._create_database(Path(tmp) / "index.sqlite")
            count = sync_data._parse_ksr_csv(raw, db, "https://example.test/ksr", "ksr.csv")
            rows = db.execute("SELECT code,kind,unit FROM resources ORDER BY code").fetchall()
            db.close()
        self.assertEqual(count, 2)
        self.assertEqual(rows, [("01.2-0001", "materials", "м3"), ("91.01.01-014", "machines", "маш.-ч")])

    def test_fsnb_zip_indexes_norm_resources_and_basis(self):
        xml = '''<?xml version="1.0" encoding="utf-8"?>
        <base CreationDate="01.08.2026" BaseName="ГЭСН доп. 19" BaseType="ГЭСН" PriceLevel="01.01.2022">
          <Section Type="Таблица" Code="01-01-001" Name="Разработка грунта">
            <NameGroup BeginName="Экскаватором: "/>
            <Work Code="01-01-001-01" EndName="группа грунтов 1" MeasureUnit="1000 м3">
              <Content><Item Text="Разработка грунта."/></Content>
              <Resources>
                <Resource Code="1-100-30" EndName="Средний разряд работы 3,0" Quantity="2"/>
                <AbstractResource Code="16.1.01" Name="Песок" MeasureUnit="м3" Quantity="П"/>
              </Resources>
              <NrSp><ReasonItem Nr="Пр/812-001.1" Sp="Пр/774-001.1"/></NrSp>
            </Work>
          </Section>
        </base>'''.encode("utf-8")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("ГЭСН.xml", xml)
        with tempfile.TemporaryDirectory() as tmp:
            db = sync_data._create_database(Path(tmp) / "index.sqlite")
            counts = sync_data._parse_fsnb_zip(buffer.getvalue(), db, "https://example.test/fsnb", "fsnb.zip")
            db.commit()
            found = db.execute("SELECT code,name,creation_date,contents,normative_basis FROM norms").fetchone()
            resource = db.execute("SELECT name FROM norm_resources").fetchone()
            db.close()
        self.assertEqual(counts[:2], (1, 1))
        self.assertEqual(found[0], "01-01-001-01")
        self.assertIn("Экскаватором", found[1])
        self.assertEqual(found[2], "01.08.2026")
        self.assertIn("Пр/812-001.1", found[4])
        self.assertIn("Средний разряд", resource[0])


class LocalIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_index = local_index.INDEX_PATH
        self.old_manifest = local_index.MANIFEST_PATH
        local_index.INDEX_PATH = Path(self.tmp.name) / "index.sqlite"
        local_index.MANIFEST_PATH = Path(self.tmp.name) / "manifest.json"
        db = sync_data._create_database(local_index.INDEX_PATH)
        db.execute(
            "INSERT INTO resources VALUES(?,?,?,?,?,?,?)",
            ("91.01.01-014", "Бульдозер мощностью 79 кВт", "маш.-ч", "machines", "", "https://source.test", "ksr.csv"),
        )
        db.execute(
            "INSERT INTO resources_fts(rowid,code,name,okpd2) SELECT rowid,code,name,okpd2 FROM resources"
        )
        db.execute(
            "INSERT INTO norms(code,name,unit,base_type,base_name,price_level,creation_date,source_file,contents,normative_basis,source_url,source_name,search_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("01-01-001-01", "Разработка грунта экскаватором", "1000 м3", "ГЭСН", "ФСНБ-2022", "01.01.2022", "01.08.2026", "ГЭСН.xml", json.dumps(["Разработка грунта"]), json.dumps([{"Nr":"Пр/812-001"}]), "https://source.test", "fsnb.zip", "01-01-001-01 Разработка грунта экскаватором"),
        )
        norm_id = db.execute("SELECT rowid FROM norms").fetchone()[0]
        db.execute("INSERT INTO norms_fts(rowid,code,name,search_text) VALUES(?,?,?,?)", (norm_id,"01-01-001-01","Разработка грунта экскаватором","01-01-001-01 Разработка грунта экскаватором"))
        norm_resource_id = db.execute(
            "INSERT INTO norm_resources(norm_rowid,code,name,unit,quantity,kind) VALUES(?,?,?,?,?,?)",
            (norm_id,"16.1.01","Песок строительный","м3","П","materials"),
        ).lastrowid
        db.execute("INSERT INTO norm_resources_fts(rowid,code,name,unit) VALUES(?,?,?,?)", (norm_resource_id,"16.1.01","Песок строительный","м3"))
        db.commit()
        db.close()

    def tearDown(self):
        local_index.INDEX_PATH = self.old_index
        local_index.MANIFEST_PATH = self.old_manifest
        self.tmp.cleanup()

    def test_resource_search_filters_and_cites_source(self):
        rows = local_index.search_resources("бульдоз", "machines")
        self.assertEqual(rows[0]["code"], "91.01.01-014")
        self.assertEqual(rows[0]["source_url"], "https://source.test")

    def test_norm_search_returns_structured_composition(self):
        rows = local_index.search_norms("грунта экскаватором")
        self.assertEqual(rows[0]["code"], "01-01-001-01")
        self.assertEqual(rows[0]["contents"], ["Разработка грунта"])

    def test_norm_search_includes_matches_from_resource_composition(self):
        rows = local_index.search_norms("песок")
        self.assertEqual(rows[0]["code"], "01-01-001-01")
        self.assertEqual(rows[0]["resources"][0]["name"], "Песок строительный")

    def test_coefficient_search_returns_reference_not_value(self):
        rows = local_index.find_coefficient_references("грунта")
        self.assertEqual(rows[0]["availability"], "basis_reference_only")
        self.assertIn("Пр/812-001", rows[0]["normative_basis"][0]["Nr"])


class PublicApiTest(unittest.TestCase):
    def test_price_filters_choose_latest_defaults_and_reject_ambiguous_zone(self):
        responses = {
            "EstimatedPrice/CountrySubjects": [{"id": 1, "name": "Новосибирская область"}],
            "EstimatedPrice/PriceZones": [{"id": 10, "name": "1 зона"}, {"id": 11, "name": "2 зона"}],
            "EstimatedPrice/Periods": [{"id": 9, "name": "3 квартал 2026 г."}],
            "EstimatedPrice/Authorities": [],
        }
        with patch.object(portal, "get_json", side_effect=lambda path, params=None: responses[path]):
            with self.assertRaises(portal.PortalError):
                portal.resolve_price_filters("Новосибирская")

    def test_price_filters_accept_explicit_region_zone_period(self):
        responses = {
            "EstimatedPrice/CountrySubjects": [{"id": 1, "name": "Новосибирская область"}],
            "EstimatedPrice/PriceZones": [{"id": 10, "name": "1 зона"}, {"id": 11, "name": "2 зона"}],
            "EstimatedPrice/Periods": [{"id": 9, "name": "3 квартал 2026 г."}],
            "EstimatedPrice/Authorities": [{"id": 8, "name": "Минстрой"}],
        }
        with patch.object(portal, "get_json", side_effect=lambda path, params=None: responses[path]):
            filters = portal.resolve_price_filters("Новосибирская", "3 квартал 2026", "1 зона", "Минстрой")
        self.assertEqual(filters["zone"]["id"], 10)
        self.assertEqual(filters["period"]["id"], 9)
        self.assertEqual(filters["authority"]["id"], 8)

    def test_price_search_uses_public_nested_price_table_for_materials_and_machines(self):
        filters = {
            "subject": {"id": 1, "name": "Регион"},
            "zone": {"id": 2, "name": "Зона"},
            "period": {"id": 3, "name": "2 квартал 2026"},
            "authority": {"id": 4, "name": "Организация"},
        }
        expected_row = {
            "code": "01.1-0001", "name": "Цемент", "unitName": "т",
            "aggregatedPrice": "123.45", "estimatedPrice": "123.45",
        }
        for kind, endpoint in (
            ("materials", "EstimatedPrice/BuildingResources/Search/Materials"),
            ("machines", "EstimatedPrice/BuildingResources/Search/Machines"),
        ):
            with self.subTest(kind=kind):
                with patch.object(portal, "get_json", side_effect=[
                    [{"id": 55, "title": "01.1-0001", "description": "Цемент"}],
                    {"items": [{"id": 1, "ksrType": 1, "items": [expected_row], "total": 1}]},
                ]) as get_json:
                    rows = portal.search_public_prices("цемент", filters, kind)
                self.assertEqual(rows, [expected_row])
                self.assertEqual(get_json.call_args_list[0].args[0], "EstimatedPrice/BuildingResources/Search")
                self.assertEqual(get_json.call_args_list[1].args[0], endpoint)
                table_params = get_json.call_args_list[1].args[1]
                self.assertEqual(table_params["value"], 55)
                self.assertEqual(table_params["authorityId"], 4)
                self.assertEqual(table_params["materials"], kind == "materials")
                self.assertEqual(table_params["machines"], kind == "machines")

    def test_price_search_returns_empty_for_no_suggestions(self):
        filters = {"subject": {"id": 1}, "zone": {"id": 2}, "period": {"id": 3}, "authority": None}
        with patch.object(portal, "get_json", return_value=[]):
            self.assertEqual(portal.search_public_prices("несуществующий ресурс", filters), [])

    def test_price_search_rejects_malformed_suggestions(self):
        filters = {"subject": {"id": 1}, "zone": {"id": 2}, "period": {"id": 3}, "authority": None}
        for payload, message in (({"unexpected": []}, "неподдерживаемую структуру"), ([{"title": "без кода"}], "без идентификатора")):
            with self.subTest(payload=payload), patch.object(portal, "get_json", return_value=payload):
                with self.assertRaisesRegex(portal.PortalError, message):
                    portal.search_public_prices("цемент", filters)

    def test_price_search_rejects_unknown_kind(self):
        filters = {"subject": {"id": 1}, "zone": {"id": 2}, "period": {"id": 3}, "authority": None}
        with patch.object(portal, "get_json") as get_json:
            with self.assertRaisesRegex(portal.PortalError, "Тип цены"):
                portal.search_public_prices("цемент", filters, "equipment")
        get_json.assert_not_called()

    def test_price_search_rejects_malformed_table_response(self):
        filters = {"subject": {"id": 1}, "zone": {"id": 2}, "period": {"id": 3}, "authority": None}
        with patch.object(portal, "get_json", side_effect=[
            [{"id": 55, "title": "01.1-0001"}],
            {"items": [{"id": 1, "total": 1}]},
        ]):
            with self.assertRaisesRegex(portal.PortalError, "без списка строк"):
                portal.search_public_prices("цемент", filters)

    def test_price_search_preserves_http_failure(self):
        filters = {
            "subject": {"id": 1}, "zone": {"id": 2}, "period": {"id": 3}, "authority": None,
        }
        with patch.object(portal, "get_json", side_effect=[
            [{"id": 55, "title": "01.1-0001", "description": "Цемент"}],
            portal.PortalError("ФГИС ЦС вернула HTTP 403 для EstimatedPrice/BuildingResources/Search/Materials."),
        ]):
            with self.assertRaisesRegex(portal.PortalError, "HTTP 403"):
                portal.search_public_prices("цемент", filters)

    def test_index_search_resolves_suggestions_to_index_rows(self):
        filters = {
            "subject": {"id": 1}, "zone": {"id": 2}, "period": {"id": 3}, "authority": None,
        }
        expected = {"id": 55, "fsbcFullCode": "01.1-0001", "index": "1.08", "price": "123.45"}

        def response(path, params=None):
            if path.endswith("/Search"):
                return [{"id": 55, "title": "01.1-0001", "description": "Цемент"}]
            if path.endswith("GetTreeViewResourceGroups"):
                return [{"value": 7, "text": "1 Цемент"}]
            if path.endswith("GetTreeViewResourcesInGroup"):
                return [expected]
            raise AssertionError(f"Unexpected endpoint: {path}")

        with patch.object(portal, "get_json", side_effect=response) as get_json:
            rows = portal.search_public_indices("цемент", filters)
        self.assertEqual(rows, [expected])
        self.assertEqual(get_json.call_count, 3)
        self.assertEqual(get_json.call_args_list[0].args[1]["subjectId"], 1)
        self.assertEqual(get_json.call_args_list[2].args[1]["searchResourceId"], 55)

    def test_index_search_falls_back_to_machine_tree(self):
        filters = {
            "subject": {"id": 1}, "zone": {"id": 2}, "period": {"id": 3}, "authority": None,
        }
        expected = {"id": 88, "fsbcFullCode": "91.01.01-014", "index": "1.08"}

        def response(path, params=None):
            if path.endswith("/Search"):
                return [{"id": 88, "title": "91.01.01-014", "description": "Бульдозер"}]
            if path.endswith("GetTreeViewResourceGroups"):
                return [] if params["isMaterials"] else [{"value": 9, "text": "Машины"}]
            if path.endswith("GetTreeViewResourcesInGroup"):
                return [expected]
            raise AssertionError(f"Unexpected endpoint: {path}")

        with patch.object(portal, "get_json", side_effect=response) as get_json:
            rows = portal.search_public_indices("бульдозер", filters)
        self.assertEqual(rows, [expected])
        self.assertFalse(get_json.call_args_list[2].args[1]["isMaterials"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
