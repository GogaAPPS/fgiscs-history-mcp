#!/usr/bin/env python3
"""Tests for the fgiscs-history MCP server.

    python3 test_server.py

These check the promises the tools make to the model calling them: that a claim
about "nothing changed" is backed by identical checksums, that an unknown input
gets a helpful answer instead of a crash, and that every answer names its source.
"""
import asyncio
import json
import re
import unittest
from unittest.mock import patch

import server

RANK_LINE = re.compile(r"^\s+[+-]\s*\d+\.\d%\s+\S")


def call(tool, **kwargs):
    return asyncio.run(tool(**kwargs))


def _ranked_lines(text):
    return [l for l in text.splitlines() if RANK_LINE.match(l)]


class DataIntegrityTest(unittest.TestCase):
    def test_every_version_belongs_to_a_listed_dataset(self):
        listed = {d["dataset"] for d in server.DATASETS}
        seen = {v["dataset"] for v in server.VERSIONS}
        self.assertEqual(seen - listed, set(), "версии без набора в datasets.json")

    def test_version_counts_match_between_files(self):
        counts = {}
        for v in server.VERSIONS:
            counts[v["dataset"]] = counts.get(v["dataset"], 0) + 1
        for d in server.DATASETS:
            self.assertEqual(d["versions"], counts[d["dataset"]], d["dataset"])

    def test_duplicate_flag_is_backed_by_equal_checksums(self):
        # Заявление «содержимое не изменилось» обязано опираться на совпадение sha256.
        flagged = [v for v in server.VERSIONS if v["duplicate_of_another_version"]]
        self.assertTrue(flagged, "в данных должен быть хотя бы один дубликат")
        for v in flagged:
            same = [o for o in server.VERSIONS if o["sha256"] == v["sha256"]]
            self.assertGreater(len(same), 1, f"{v['filename'] if 'filename' in v else v}")

    def test_no_duplicate_flag_without_a_twin(self):
        by_hash = {}
        for v in server.VERSIONS:
            by_hash.setdefault(v["sha256"], []).append(v)
        for h, group in by_hash.items():
            expected = len(group) > 1
            for v in group:
                self.assertEqual(v["duplicate_of_another_version"], expected, h[:12])

    def test_every_version_links_to_its_source(self):
        for v in server.VERSIONS:
            self.assertTrue(v["source_url"].startswith("https://fgiscs.minstroyrf.ru/"),
                            v["source_url"])


class ToolTest(unittest.TestCase):
    def test_list_datasets_names_the_source(self):
        out = call(server.list_datasets)
        self.assertIn("fgiscs.minstroyrf.ru", out)
        self.assertIn("Классификатор строительных ресурсов", out)

    def test_dataset_versions_marks_schema_change_and_duplicate(self):
        out = call(server.dataset_versions, dataset="7707082071-ksrms")
        self.assertIn("СХЕМА ИЗМЕНИЛАСЬ на 20221117", out)
        self.assertIn("СОДЕРЖИМОЕ НЕ ИЗМЕНИЛОСЬ", out)

    def test_dataset_versions_on_unknown_id_lists_valid_ones(self):
        out = call(server.dataset_versions, dataset="нет-такого")
        self.assertIn("не найден", out)
        self.assertIn("7707082071-ksrms", out, "подсказать существующие идентификаторы")

    def test_salary_history_returns_series_and_growth(self):
        out = call(server.salary_history, subject="Иркутская")
        self.assertIn("Иркутская область", out)
        self.assertIn("20200425", out)
        self.assertIn("итого за период:", out)
        self.assertIn("fgiscs.minstroyrf.ru", out)

    def test_salary_history_is_case_insensitive_and_partial(self):
        self.assertIn("Москва", call(server.salary_history, subject="москв"))

    def test_salary_history_on_unknown_region_suggests_instead_of_failing(self):
        out = call(server.salary_history, subject="Атлантида")
        self.assertIn("не найден", out)
        self.assertNotIn("Traceback", out)

    def test_search_current_prices_returns_rows_and_filter_metadata(self):
        filters = {
            "subject": {"id": 1, "name": "Новосибирская область"},
            "zone": {"id": 2, "name": "1 зона"},
            "period": {"id": 3, "name": "2 квартал 2026"},
            "authority": {"id": 4, "name": "Минстрой"},
        }
        row = {"code": "01.7.03.01-0002", "name": "Вода водопроводная", "unitName": "м3", "aggregatedPrice": "32.96"}
        for kind in ("materials", "machines"):
            with self.subTest(kind=kind):
                with patch.object(server, "resolve_price_filters", return_value=filters), patch.object(
                    server, "search_public_prices", return_value=[row]
                ) as search:
                    output = call(
                        server.search_current_prices,
                        query="вода" if kind == "materials" else "экскаватор",
                        subject="Новосибирская область",
                        period="2 квартал 2026",
                        price_zone="1 зона",
                        authority="Минстрой",
                        kind=kind,
                    )
                result = json.loads(output)
                self.assertEqual(result["items"], [row])
                self.assertEqual(result["kind"], kind)
                self.assertEqual(result["subject"], "Новосибирская область")
                self.assertEqual(result["price_zone"], "1 зона")
                self.assertEqual(result["period"], "2 квартал 2026")
                self.assertTrue(result["source"].endswith("/prices"))
                self.assertTrue(result["retrieved_at"])
                self.assertEqual(search.call_args.args[2], kind)

    def test_search_base_resource_prices_returns_components_and_source(self):
        row = {
            "code": "01.7.03.01-0002",
            "name": "Вода водопроводная",
            "unit": "м3",
            "kind": "materials",
            "source_file": "ФСБЦ_Мат&Оборуд.xml",
            "edition": "20260812",
            "price_level": "базисный",
            "components": {"prices": [{"Cost": "12.5", "OptCost": "14.0"}]},
            "source_url": "https://fgiscs.minstroyrf.ru/source.zip",
            "source_name": "ФСНБ-2022",
        }
        with patch.object(server, "query_base_prices", return_value=[row]) as search:
            output = call(server.search_base_resource_prices, query="вода", kind="materials")
        self.assertIn("ФСБЦ_Мат&Оборуд.xml", output)
        self.assertIn("20260812", output)
        self.assertIn('"OptCost": "14.0"', output)
        self.assertIn(row["source_url"], output)
        search.assert_called_once_with("вода", "materials", 10)

    def test_search_norms_batch_forwards_unit_and_returns_contract_rows(self):
        row = {
            "code": "11-01-047-01",
            "name": "Устройство покрытий из плит керамогранитных",
            "unit": "100 м2",
            "creation_date": "14.08.2026",
            "source_url": "https://fgiscs.minstroyrf.ru/source.zip",
        }
        with patch.object(server, "query_norms", return_value=[row]) as search:
            output = call(
                server.search_norms_batch,
                queries=[{"id": "position-1", "text": "Укладка керамогранита", "unit": "м2"}],
                limit=5,
            )
        result = json.loads(output)
        self.assertEqual(result["contract_version"], "1.0")
        self.assertEqual(result["results"][0]["query_id"], "position-1")
        self.assertEqual(result["results"][0]["matches"][0]["code"], row["code"])
        search.assert_called_once_with("Укладка керамогранита", 5, expected_unit="м2")

    def test_search_current_labor_prices_returns_unit_filters_and_source(self):
        filters = {
            "subject": {"id": 1, "name": "Новосибирская область"},
            "zone": {"id": 2, "name": "1 зона"},
            "period": {"id": 3, "name": "3 квартал 2026"},
            "authority": {"id": 4, "name": "Минстрой"},
        }
        row = {"code": "1-100-10", "salaryRateRank": "1.0", "salary": "444.77"}
        with patch.object(server, "resolve_price_filters", return_value=filters), patch.object(
            server, "search_public_labor_prices", return_value=[row]
        ) as search:
            output = call(
                server.search_current_labor_prices,
                query="1-100-10", subject="Новосибирская область", period="3 квартал 2026",
                price_zone="1 зона", authority="Минстрой",
            )
        result = json.loads(output)
        self.assertEqual(result["items"], [row])
        self.assertEqual(result["unit"], "руб./чел.-ч")
        self.assertEqual(result["subject"], filters["subject"]["name"])
        self.assertTrue(result["source"].endswith("/prices"))
        self.assertTrue(result["api"].endswith("/RimWorkerSalaryRegistry"))
        search.assert_called_once_with("1-100-10", filters, 10)

    def test_search_current_transport_prices_returns_source_and_filters(self):
        filters = {
            "subject": {"id": 1, "name": "Новосибирская область"},
            "zone": {"id": 2, "name": "1 зона"},
            "period": {"id": 3, "name": "3 квартал 2026"},
            "authority": {"id": 4, "name": "РОСАТОМ"},
        }
        row = {"code": "1", "interval": "до 10 км", "price": "19.50"}
        with patch.object(server, "resolve_price_filters", return_value=filters), patch.object(
            server, "search_public_transport_prices", return_value=[row]
        ) as search:
            output = call(
                server.search_current_transport_prices,
                query="грунтовые", subject="Новосибирская область", authority="РОСАТОМ",
                transport_type="rail", road_type="грунтовые", limit=5,
            )
        result = json.loads(output)
        self.assertEqual(result["items"], [row])
        self.assertEqual(result["unit"], "руб./т; тарифный интервал расстояния указан в строке")
        self.assertEqual(result["transport_type"], "rail")
        self.assertEqual(result["applied_filters"]["road_type"], "грунтовые")
        self.assertTrue(result["source"].endswith("/prices"))
        search.assert_called_once_with("грунтовые", filters, "rail", "грунтовые", None, None, 5)

    def test_search_current_building_indices_is_separate_from_resource_group_indices(self):
        filters = {
            "subject": {"id": 1, "name": "Новосибирская область"},
            "zone": {"id": 2, "name": "1 зона"},
            "period": {"id": 3, "name": "3 квартал 2026"},
            "authority": None,
        }
        row = {"buildingTypeName": "Жилые здания", "indexFer": "7.81"}
        with patch.object(server, "resolve_price_filters", return_value=filters), patch.object(
            server, "search_public_building_indices", return_value=[row]
        ) as search:
            output = call(
                server.search_current_building_indices,
                query="жилые", subject="Новосибирская область", index_type="building_types",
            )
        result = json.loads(output)
        self.assertEqual(result["items"], [row])
        self.assertEqual(result["index_type"], "building_types")
        self.assertEqual(result["authority"], "")
        self.assertTrue(result["retrieved_at"])
        search.assert_called_once_with("жилые", filters, "building_types", 10)

    def test_ranking_respects_limit_and_direction(self):
        top = call(server.salary_growth_ranking, limit=3)
        self.assertEqual(len(_ranked_lines(top)), 3)
        bottom = call(server.salary_growth_ranking, limit=3, ascending=True)
        self.assertIn("Ненецкий автономный округ", bottom)
        self.assertNotIn("Ненецкий автономный округ", top)

    def test_ranking_limit_is_clamped(self):
        # Модель может передать что угодно; падать из-за этого сервер не должен.
        self.assertIn("Рост среднемесячной", call(server.salary_growth_ranking, limit=10**6))
        self.assertIn("Рост среднемесячной", call(server.salary_growth_ranking, limit=0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
