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
    return re.findall(r"[\w]+", query, flags=re.UNICODE)


def _fts_query(query: str) -> str:
    return " AND ".join('"' + token.replace('"', '""') + '"*' for token in _tokens(query))


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


def search_norms(query: str, limit: int = 10, norm_code: str | None = None) -> list[dict[str, Any]]:
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
            expression = _fts_query(query)
            norm_ids = [row[0] for row in db.execute(
                "SELECT rowid FROM norms_fts WHERE norms_fts MATCH ? LIMIT ?",
                (expression, limit),
            ).fetchall()]
            resource_ids = [row[0] for row in db.execute(
                "SELECT DISTINCT r.norm_rowid FROM norm_resources_fts f "
                "JOIN norm_resources r ON r.id=f.rowid "
                "WHERE norm_resources_fts MATCH ? LIMIT ?",
                (expression, limit),
            ).fetchall()]
            row_ids = list(dict.fromkeys(norm_ids + resource_ids))[:limit]
        results = []
        if row_ids:
            placeholders = ",".join("?" for _ in row_ids)
            rows = db.execute(
                f"SELECT rowid,* FROM norms WHERE rowid IN ({placeholders}) ORDER BY code,base_type",
                row_ids,
            ).fetchall()
            for row in rows:
                item = dict(row)
                norm_rowid = item.pop("rowid")
                for field in ("contents", "normative_basis"):
                    item[field] = json.loads(item[field])
                item["resources"] = [dict(resource) for resource in db.execute(
                    "SELECT code,name,unit,quantity,kind FROM norm_resources WHERE norm_rowid=? ORDER BY id",
                    (norm_rowid,),
                ).fetchall()]
                item.pop("search_text", None)
                results.append(item)
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
