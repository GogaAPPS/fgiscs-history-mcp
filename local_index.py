"""Read-only queries over the optional local FGIS CS search index."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CACHE_DIR = Path(os.environ.get("FGISCS_CACHE_DIR", ROOT / "data" / "cache"))
INDEX_PATH = CACHE_DIR / "index.sqlite"
MANIFEST_PATH = CACHE_DIR / "manifest.json"


def _db() -> sqlite3.Connection:
    if not INDEX_PATH.exists():
        raise FileNotFoundError("Локальный индекс ещё не создан. Запустите python sync_data.py.")
    connection = sqlite3.connect(f"file:{INDEX_PATH}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _tokens(query: str) -> list[str]:
    return re.findall(r"[\w]+", query.casefold().replace("ё", "е"), flags=re.UNICODE)


def _fts_query(query: str) -> str:
    return " AND ".join('"' + token.replace('"', '""') + '"*' for token in _tokens(query))


_SEARCH_STOP_WORDS = {
    "без", "в", "для", "до", "и", "из", "или", "на", "от", "по", "при", "с", "со",
    "старого", "старой", "старых", "работа", "работы",
}

# ФСНБ называет операции нормативными терминами (например, «разборка» и
# «устройство»), тогда как пользователь обычно пишет «снятие» и «укладка».
# Это небольшой контролируемый словарь понятий, а не список готовых запросов:
# исходные слова всегда имеют больший вес при итоговом ранжировании.
_NORMATIVE_TERMS = {
    "замена": ("смена",),
    "освещение": ("светильник", "осветительный"),
    "освещения": ("светильник", "осветительный"),
    "покраска": ("окраска",),
    "снятие": ("разборка", "демонтаж"),
    "укладка": ("покрытие", "настилка"),
    "выравнивание": ("устройство", "стяжка"),
}


def _search_stem(token: str) -> str:
    """Return a conservative Russian word prefix suitable for FTS5 prefix search."""
    token = token.casefold().replace("ё", "е")
    for ending in (
        "иями", "ями", "ами", "ного", "ному", "ными", "ого", "ему", "ому", "иями",
        "ных", "ную", "ная", "ное", "ные", "ный", "иях", "ыми", "ими", "ие",
        "ах", "ях", "ек", "ов", "ев", "ий", "ый", "ая", "ое", "ые", "ам", "ям",
        "ом", "ем", "а", "я", "ы", "и", "е", "у", "ю",
    ):
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[: -len(ending)]
    return token


def _meaningful_terms(query: str) -> list[str]:
    return list(dict.fromkeys(
        _search_stem(token)
        for token in _tokens(query)
        if len(token) > 2 and token not in _SEARCH_STOP_WORDS
    ))


def _expanded_terms(query: str) -> list[str]:
    terms = _meaningful_terms(query)
    expanded = list(terms)
    for token in _tokens(query):
        for synonym in _NORMATIVE_TERMS.get(token, ()):
            stem = _search_stem(synonym)
            if stem not in expanded:
                expanded.append(stem)
    return expanded


def _query_concepts(query: str) -> list[set[str]]:
    concepts = []
    for token in _tokens(query):
        if len(token) <= 2 or token in _SEARCH_STOP_WORDS:
            continue
        concepts.append({
            _search_stem(token),
            *(_search_stem(term) for term in _NORMATIVE_TERMS.get(token, ())),
        })
    return concepts


def _fts_terms(terms: list[str], operator: str) -> str:
    return f" {operator} ".join('"' + term.replace('"', '""') + '"*' for term in terms)


def _concept_expression(concepts: list[set[str]]) -> str:
    groups = [f"({_fts_terms(sorted(concept), 'OR')})" for concept in concepts]
    return " AND ".join(groups)


def _text_terms(value: str) -> set[str]:
    return {_search_stem(token) for token in _tokens(value) if len(token) > 2}


def _unit_key(value: str) -> str:
    value = value.casefold().replace("²", "2").replace("³", "3")
    value = re.sub(r"^\s*\d+(?:[.,]\d+)?\s*", "", value)
    return re.sub(r"[^\w]+", "", value)


def _norm_relevance(
    query: str,
    row: sqlite3.Row,
    resources: list[dict[str, Any]],
    expected_unit: str | None,
) -> tuple[int, int]:
    original = set(_meaningful_terms(query))
    expanded = set(_expanded_terms(query)) - original
    name_sequence = [_search_stem(token) for token in _tokens(str(row["name"]))]
    name_terms = _text_terms(str(row["name"]))
    content_terms = _text_terms(str(row["search_text"]))
    resource_terms = _text_terms(" ".join(str(item.get("name", "")) for item in resources))
    exact_name = len(original & name_terms)
    exact_all = len(original & (content_terms | resource_terms))
    synonym_all = len(expanded & (content_terms | resource_terms))
    concepts = _query_concepts(query)
    concept_matches = sum(bool(concept & name_terms) for concept in concepts)
    action_first = bool(concepts and concepts[0] & set(name_sequence[:8]))
    coverage = round(20 * exact_all / max(1, len(original)))
    score = (
        coverage
        + concept_matches * 100
        + (100 if action_first else 0)
        + exact_name * 5
        + exact_all * 2
        + synonym_all * 5
    )
    requested_unit = _unit_key(expected_unit or "")
    candidate_unit = _unit_key(str(row["unit"]))
    if requested_unit and candidate_unit:
        score += 100 if requested_unit == candidate_unit else -50
    return score, exact_name


def _manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def sync_status() -> str:
    manifest = _manifest()
    if not manifest:
        return "Локальных выгрузок пока нет. Выполните `python sync_data.py` для первичной синхронизации."
    lines = [f"Снимок получен: {manifest.get('retrieved_at', 'не указано')}"]
    for dataset in manifest.get("datasets", []):
        lines.append(
            f"{dataset.get('title')} — публикация {dataset.get('last_change_date')}; "
            f"получено {dataset.get('retrieved_at')}; файл {dataset.get('file_name')}; "
            f"{dataset.get('file_bytes', 0):,} байт; SHA-256 {dataset.get('file_sha256', '')[:12]}; "
            f"источник {dataset.get('source_url')}"
        )
        if dataset.get("base_edition"):
            lines.append(f"  Редакция ФСНБ: {dataset['base_edition']}; норм проиндексировано: {dataset.get('norms_indexed', 0):,}")
        if dataset.get("resources_indexed"):
            lines.append(f"  Строк ресурсов: {dataset['resources_indexed']:,}")
        if dataset.get("base_prices_indexed") is not None:
            lines.append(f"  Базисные цены ресурсов: {dataset['base_prices_indexed']:,}")
    return "\n".join(lines)


def search_resources(query: str, kind: str = "all", limit: int = 10) -> list[dict[str, Any]]:
    if not _tokens(query):
        raise ValueError("Введите код или название ресурса для поиска.")
    if kind not in {"all", "materials", "machines", "labor"}:
        raise ValueError("Тип ресурса должен быть all, materials, machines или labor.")
    with _db() as db:
        expression = _fts_query(query)
        sql = (
            "SELECT r.code,r.name,r.unit,r.kind,r.okpd2,r.source_url,r.source_name "
            "FROM resources_fts f JOIN resources r ON r.rowid=f.rowid "
            "WHERE resources_fts MATCH ?"
        )
        params: list[Any] = [expression]
        if kind != "all":
            sql += " AND r.kind=?"
            params.append(kind)
        limit = max(1, min(int(limit), 25))
        sql += " ORDER BY CASE WHEN r.code=? THEN 0 WHEN r.code LIKE ? THEN 1 ELSE 2 END,r.code LIMIT ?"
        params.extend((query.strip(), query.strip() + "%", limit))
        return [dict(row) for row in db.execute(sql, params).fetchall()]


def search_base_prices(query: str, kind: str = "all", limit: int = 10) -> list[dict[str, Any]]:
    """Search source-reported FSBC base-price components by code or name."""
    if not _tokens(query):
        raise ValueError("Введите код или название ресурса для поиска базисной цены.")
    if kind not in {"all", "materials", "machines"}:
        raise ValueError("Тип базисной цены должен быть all, materials или machines.")
    limit = max(1, min(int(limit), 25))
    try:
        with _db() as db:
            sql = (
                "SELECT b.code,b.name,b.unit,b.kind,b.source_file,b.edition,b.price_level,"
                "b.components,b.source_url,b.source_name "
                "FROM base_prices_fts f JOIN base_prices b ON b.id=f.rowid "
                "WHERE base_prices_fts MATCH ?"
            )
            params: list[Any] = [_fts_query(query)]
            if kind != "all":
                sql += " AND b.kind=?"
                params.append(kind)
            sql += " ORDER BY CASE WHEN b.code=? THEN 0 WHEN b.code LIKE ? THEN 1 ELSE 2 END,b.code LIMIT ?"
            params.extend((query.strip(), query.strip() + "%", limit))
            results = []
            for row in db.execute(sql, params).fetchall():
                item = dict(row)
                item["components"] = json.loads(item["components"])
                results.append(item)
            return results
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            raise ValueError(
                "Локальная SQLite создана старой версией схемы; пересоберите индекс командой "
                "`python sync_data.py --reindex` (или `python sync_data.py`, чтобы также обновить выгрузку)."
            ) from exc
        raise


def search_norms(
    query: str,
    limit: int = 10,
    norm_code: str | None = None,
    expected_unit: str | None = None,
) -> list[dict[str, Any]]:
    if not _tokens(query) and not norm_code:
        raise ValueError("Введите код или текст для поиска сметной нормы.")
    limit = max(1, min(int(limit), 20))
    with _db() as db:
        if norm_code:
            row_ids = [row[0] for row in db.execute(
                "SELECT rowid FROM norms WHERE code LIKE ? ORDER BY code,base_type LIMIT ?",
                (norm_code.strip() + "%", limit),
            ).fetchall()]
        else:
            terms = _meaningful_terms(query)
            if not terms:
                raise ValueError("Введите содержательное описание работы.")
            concepts = _query_concepts(query)
            expanded_terms = _expanded_terms(query)
            expressions = [_concept_expression(concepts), _fts_terms(terms, "AND")]
            for left in range(len(concepts)):
                for right in range(left + 1, len(concepts)):
                    expressions.append(_concept_expression([concepts[left], concepts[right]]))
            expressions.extend(_fts_terms([term], "OR") for term in expanded_terms)
            expressions = list(dict.fromkeys(expressions))
            row_ids = []
            candidate_limit = max(100, limit * 20)
            for expression in expressions:
                norm_ids = [row[0] for row in db.execute(
                    "SELECT rowid FROM norms_fts WHERE norms_fts MATCH ? LIMIT ?",
                    (expression, candidate_limit),
                ).fetchall()]
                resource_ids = [row[0] for row in db.execute(
                    "SELECT DISTINCT r.norm_rowid FROM norm_resources_fts f "
                    "JOIN norm_resources r ON r.id=f.rowid "
                    "WHERE norm_resources_fts MATCH ? LIMIT ?",
                    (expression, candidate_limit),
                ).fetchall()]
                row_ids = list(dict.fromkeys(row_ids + norm_ids + resource_ids))
        results = []
        if row_ids:
            placeholders = ",".join("?" for _ in row_ids)
            rows = db.execute(f"SELECT rowid,* FROM norms WHERE rowid IN ({placeholders})", row_ids).fetchall()
            ranked_rows = []
            for row in rows:
                item = dict(row)
                norm_rowid = item.pop("rowid")
                for field in ("contents", "normative_basis"):
                    item[field] = json.loads(item[field])
                item["resources"] = [dict(resource) for resource in db.execute(
                    "SELECT code,name,unit,quantity,kind FROM norm_resources WHERE norm_rowid=? ORDER BY id",
                    (norm_rowid,),
                ).fetchall()]
                relevance = (
                    (0, 0)
                    if norm_code
                    else _norm_relevance(query, row, item["resources"], expected_unit)
                )
                item.pop("search_text", None)
                ranked_rows.append((relevance, str(item["code"]), item))
            ranked_rows.sort(key=lambda value: (-value[0][0], -value[0][1], value[1]))
            results = [item for _, _, item in ranked_rows[:limit]]
    return results


def find_coefficient_references(query: str, norm_code: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Return only basis references present on a work norm, never infer a coefficient value."""
    norms = search_norms(query, limit=limit, norm_code=norm_code)
    results = []
    for norm in norms:
        if norm.get("normative_basis"):
            results.append({
                "norm_code": norm["code"],
                "norm_name": norm["name"],
                "normative_basis": norm["normative_basis"],
                "source_url": norm["source_url"],
                "source_name": norm["source_name"],
                "availability": "basis_reference_only",
            })
    return results
