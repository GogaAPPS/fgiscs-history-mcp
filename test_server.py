#!/usr/bin/env python3
"""Tests for the fgiscs-history MCP server.

    python3 test_server.py

These check the promises the tools make to the model calling them: that a claim
about "nothing changed" is backed by identical checksums, that an unknown input
gets a helpful answer instead of a crash, and that every answer names its source.
"""
import asyncio
import re
import unittest

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
