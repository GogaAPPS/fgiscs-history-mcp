#!/usr/bin/env python3
"""Synchronize public FGIS CS data and build the local searchable snapshot.

Run manually or from an external scheduler. The command only reads public GET
endpoints and writes the cache under data/cache/.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from portal import (
    PORTAL_BASE,
    PortalError,
    file_content_url,
    get_bytes,
    get_dataset_detail,
    list_open_datasets,
    retrieved_at,
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_DIR = Path(os.environ.get("FGISCS_CACHE_DIR", DATA_DIR / "cache")).expanduser()
DEFAULT_IDS = ("7707082071-ksrms", "7707082071-fsnb", "7707082071-OplataTruda")
MAX_DATASET_BYTES = 250 * 1024 * 1024


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _xml_text(element: ET.Element, name: str) -> str:
    child = next((item for item in element if _tag(item) == name), None)
    return (child.text or "").strip() if child is not None else ""


def _resource_kind(code: str, name: str = "") -> str:
    if code.startswith("91.") or "машин" in name.casefold() or "механизм" in name.casefold():
        return "machines"
    if code.startswith("1-") or "средний разряд работы" in name.casefold():
        return "labor"
    return "materials"


def _insert_resource(
    db: sqlite3.Connection, code: str, name: str, unit: str, source_url: str,
    source_name: str, kind: str, okpd2: str = "",
) -> None:
    if not code or not name:
        return
    db.execute(
        "INSERT OR REPLACE INTO resources(code,name,unit,kind,okpd2,source_url,source_name) VALUES(?,?,?,?,?,?,?)",
        (code.strip(), name.strip(), unit.strip(), kind, okpd2.strip(), source_url, source_name),
    )


def _parse_ksr_csv(data: bytes, db: sqlite3.Connection, source_url: str, source_name: str) -> int:
    if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-8-sig")
    else:
        try:
            text = data.decode("cp1251")
        except UnicodeDecodeError:
            text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=",")
    header = next(reader, [])
    if len(header) < 4 or "Код КСР" not in header[1]:
        raise PortalError("Неожиданная структура выгрузки КСР: не найдены ожидаемые столбцы.")
    inserted = 0
    for row in reader:
        if len(row) < 4:
            continue
        okpd2, code, name, unit = row[:4]
        if not code.strip() or not name.strip():
            continue
        _insert_resource(db, code, name, unit, source_url, source_name, _resource_kind(code, name), okpd2)
        inserted += 1
    return inserted


def _parse_fsnb_zip(data: bytes, db: sqlite3.Connection, source_url: str, source_name: str) -> tuple[int, int, str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PortalError("Выгрузка ФСНБ не является ожидаемым ZIP-архивом.") from exc
    norms_added = 0
    resources_added = 0
    editions: list[str] = []
    xml_files = [entry for entry in archive.infolist() if entry.filename.lower().endswith(".xml")]
    if not xml_files:
        raise PortalError("В архиве ФСНБ не найдены XML-файлы.")

    for info in xml_files:
        file_base = Path(info.filename).stem
        try:
            xml_stream: BinaryIO = archive.open(info)
            stack: list[dict[str, str]] = []
            root_attrs: dict[str, str] = {}
            for event, element in ET.iterparse(xml_stream, events=("start", "end")):
                tag = _tag(element)
                if event == "start":
                    if not stack and tag in {"base", "ResourceCatalog"}:
                        root_attrs = dict(element.attrib)
                    if tag == "Section":
                        stack.append(dict(element.attrib))
                    elif tag == "NameGroup":
                        if stack:
                            stack[-1]["_name_group"] = element.attrib.get("BeginName", "")
                    continue

                if tag == "Resource" and element.attrib.get("Code"):
                    attrs = element.attrib
                    _insert_resource(
                        db, attrs.get("Code", ""), attrs.get("Name") or attrs.get("EndName", ""),
                        attrs.get("MeasureUnit", ""), source_url, source_name,
                        _resource_kind(attrs.get("Code", ""), attrs.get("Name", "")),
                    )
                    resources_added += 1

                if tag == "Work":
                    attrs = element.attrib
                    code = attrs.get("Code", "").strip()
                    if code:
                        table = next((item for item in reversed(stack) if item.get("Type") == "Таблица"), None)
                        table_name = table.get("Name", "") if table else ""
                        group = next((item.get("_name_group", "") for item in reversed(stack) if item.get("_name_group")), "")
                        tail = attrs.get("EndName", "")
                        parts = [part.strip() for part in (table_name, group, tail) if part.strip()]
                        name = " — ".join(dict.fromkeys(parts))
                        contents = [child.attrib.get("Text", "").strip() for child in element.iter() if _tag(child) == "Item"]
                        norm_resources: list[dict[str, str]] = []
                        reasons: list[dict[str, str]] = []
                        for child in element.iter():
                            child_tag = _tag(child)
                            if child_tag in {"Resource", "AbstractResource"}:
                                ra = child.attrib
                                rname = ra.get("Name") or ra.get("EndName", "")
                                norm_resources.append({
                                    "code": ra.get("Code", ""),
                                    "name": rname,
                                    "unit": ra.get("MeasureUnit", ""),
                                    "quantity": ra.get("Quantity", ""),
                                    "kind": _resource_kind(ra.get("Code", ""), rname),
                                })
                            elif child_tag == "ReasonItem":
                                reasons.append(dict(child.attrib))
                        record = {
                            "code": code,
                            "name": name,
                            "unit": attrs.get("MeasureUnit", ""),
                            "base_type": root_attrs.get("BaseType", file_base),
                            "base_name": root_attrs.get("BaseName", ""),
                            "price_level": root_attrs.get("PriceLevel", ""),
                            "creation_date": root_attrs.get("CreationDate", ""),
                            "source_file": info.filename,
                            "contents": [item for item in contents if item],
                            "resources": norm_resources,
                            "normative_basis": reasons,
                            "source_url": source_url,
                            "source_name": source_name,
                        }
                        text_search = " ".join([
                            code, name, record["base_type"], record["base_name"],
                            *record["contents"],
                        ])
                        db.execute(
                            "INSERT OR REPLACE INTO norms(code,name,unit,base_type,base_name,price_level,creation_date,source_file,contents,normative_basis,source_url,source_name,search_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (code, name, record["unit"], record["base_type"], record["base_name"],
                             record["price_level"], record["creation_date"], info.filename,
                             json.dumps(record["contents"], ensure_ascii=False),
                             json.dumps(reasons, ensure_ascii=False), source_url, source_name, text_search),
                        )
                        norm_rowid = db.execute(
                            "SELECT rowid FROM norms WHERE code=? AND base_type=?", (code, record["base_type"]),
                        ).fetchone()[0]
                        db.executemany(
                            "INSERT INTO norm_resources(norm_rowid,code,name,unit,quantity,kind) VALUES(?,?,?,?,?,?)",
                            [(norm_rowid, r["code"], r["name"], r["unit"], r["quantity"], r["kind"])
                             for r in norm_resources],
                        )
                        norms_added += 1
                    element.clear()

                if tag == "NameGroup":
                    element.clear()
                elif tag == "Section" and stack:
                    stack.pop()
                    element.clear()

            edition = root_attrs.get("CreationDate") or root_attrs.get("BaseName")
            if edition:
                editions.append(edition)
        except ET.ParseError as exc:
            raise PortalError(f"Ошибка разбора {info.filename} внутри ФСНБ ZIP.") from exc
    return norms_added, resources_added, ", ".join(sorted(set(editions)))


def _create_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.executescript("""
        CREATE TABLE resources (
            code TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
            kind TEXT NOT NULL, okpd2 TEXT NOT NULL, source_url TEXT NOT NULL,
            source_name TEXT NOT NULL, PRIMARY KEY(code, source_name)
        );
        CREATE INDEX resources_code ON resources(code);
        CREATE INDEX resources_kind ON resources(kind);
        CREATE VIRTUAL TABLE resources_fts USING fts5(code,name,okpd2,content='');
        CREATE TABLE norms (
            code TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
            base_type TEXT NOT NULL, base_name TEXT NOT NULL, price_level TEXT NOT NULL,
            creation_date TEXT NOT NULL, source_file TEXT NOT NULL, contents TEXT NOT NULL,
            normative_basis TEXT NOT NULL, source_url TEXT NOT NULL,
            source_name TEXT NOT NULL, search_text TEXT NOT NULL,
            PRIMARY KEY(code,base_type)
        );
        CREATE VIRTUAL TABLE norms_fts USING fts5(code, name, search_text, content='');
        CREATE TABLE norm_resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT, norm_rowid INTEGER NOT NULL,
            code TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
            quantity TEXT NOT NULL, kind TEXT NOT NULL
        );
        CREATE INDEX norm_resources_norm ON norm_resources(norm_rowid);
        CREATE VIRTUAL TABLE norm_resources_fts USING fts5(code,name,unit,content='');
    """)
    return db


def _rebuild_fts(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO resources_fts(rowid,code,name,okpd2) "
        "SELECT rowid,code,name,okpd2 FROM resources"
    )
    db.execute(
        "INSERT INTO norms_fts(rowid,code,name,search_text) "
        "SELECT rowid,code,name,search_text FROM norms"
    )
    db.execute(
        "INSERT INTO norm_resources_fts(rowid,code,name,unit) "
        "SELECT id,code,name,unit FROM norm_resources"
    )


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = Path(name).name
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return re.sub(r"[^\w.()&-]+", "_", cleaned, flags=re.UNICODE)


def synchronize(dataset_ids: tuple[str, ...] = DEFAULT_IDS) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    catalog = list_open_datasets()
    by_id = {str(item.get("identificationNumber", "")).strip(): item for item in catalog}
    selected = []
    for dataset_id in dataset_ids:
        listed = by_id.get(dataset_id.strip())
        if not listed:
            raise PortalError(f"Набор {dataset_id!r} не найден в актуальном каталоге ФГИС ЦС.")
        selected.append((dataset_id.strip(), listed))

    catalog_path = CACHE_DIR / "catalog.json"
    catalog_doc = {"retrieved_at": retrieved_at(), "source_url": PORTAL_BASE + "/opendata", "items": catalog}
    catalog_path.write_text(json.dumps(catalog_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest: dict[str, Any] = {"retrieved_at": catalog_doc["retrieved_at"], "datasets": []}

    db_fd, db_temp_name = tempfile.mkstemp(prefix="fgiscs-index-", suffix=".sqlite", dir=CACHE_DIR)
    os.close(db_fd)
    db_temp = Path(db_temp_name)
    db = _create_database(db_temp)
    try:
        for dataset_id, listed in selected:
            detail = get_dataset_detail(dataset_id, str(listed.get("identificationNumber", "")))
            file_info = detail["datasetFile"]
            file_id = str(file_info.get("path", ""))
            if not file_id:
                raise PortalError(f"Карточка {dataset_id} не содержит file ID.")
            raw, _ = get_bytes(f"values/GetFileContent/{file_id}", MAX_DATASET_BYTES)
            display_name = _safe_filename(str(file_info.get("name", "")), dataset_id + ".bin")
            raw_path = CACHE_DIR / "files" / dataset_id / display_name
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw)
            source_url = file_content_url(file_id)
            record: dict[str, Any] = {
                "identification_number": dataset_id,
                "title": detail.get("datasetName", dataset_id),
                "last_change_date": detail.get("lastChangeDate"),
                "retrieved_at": retrieved_at(),
                "file_name": display_name,
                "file_id": file_id,
                "file_bytes": len(raw),
                "file_sha256": __import__("hashlib").sha256(raw).hexdigest(),
                "source_url": source_url,
                "local_path": str(raw_path.relative_to(CACHE_DIR)),
            }
            if dataset_id == "7707082071-ksrms":
                record["resources_indexed"] = _parse_ksr_csv(raw, db, source_url, display_name)
            elif dataset_id == "7707082071-fsnb":
                record["norms_indexed"], record["resources_indexed"], record["base_edition"] = _parse_fsnb_zip(
                    raw, db, source_url, display_name,
                )
            manifest["datasets"].append(record)
        _rebuild_fts(db)
        db.commit()
        db.execute("INSERT INTO norms_fts(norms_fts) VALUES('optimize')")
        db.execute("INSERT INTO resources_fts(resources_fts) VALUES('optimize')")
        db.execute("INSERT INTO norm_resources_fts(norm_resources_fts) VALUES('optimize')")
        db.commit()
        db.close()
        db_temp.replace(CACHE_DIR / "index.sqlite")
    except Exception:
        db.close()
        db_temp.unlink(missing_ok=True)
        raise

    manifest_path = CACHE_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def reindex_cached(dataset_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Rebuild SQLite from already-downloaded files without making network calls."""
    manifest_path = CACHE_DIR / "manifest.json"
    if not manifest_path.exists():
        raise PortalError("Нет manifest.json в кэше; сначала запустите python sync_data.py.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = set(dataset_ids or ())
    records = [item for item in manifest.get("datasets", []) if not selected or item.get("identification_number") in selected]
    if not records:
        raise PortalError("В кэше нет выгрузок для переиндексации.")
    wanted = {item.get("identification_number") for item in records}
    if len(wanted) != len(manifest.get("datasets", [])):
        raise PortalError("Переиндексация части наборов не поддерживается: индекс строится атомарно для всех записей manifest.")

    db_fd, db_temp_name = tempfile.mkstemp(prefix="fgiscs-index-", suffix=".sqlite", dir=CACHE_DIR)
    os.close(db_fd)
    db_temp = Path(db_temp_name)
    db = _create_database(db_temp)
    try:
        for record in records:
            stored_path = Path(record["local_path"])
            raw_path = stored_path if stored_path.is_absolute() else CACHE_DIR / stored_path
            if not raw_path.exists() and not stored_path.is_absolute():
                # Read manifests written before local_path became cache-relative.
                legacy_path = ROOT / stored_path
                if legacy_path.exists():
                    raw_path = legacy_path
            raw = raw_path.read_bytes()
            dataset_id = record["identification_number"]
            if dataset_id == "7707082071-ksrms":
                record["resources_indexed"] = _parse_ksr_csv(raw, db, record["source_url"], record["file_name"])
            elif dataset_id == "7707082071-fsnb":
                record["norms_indexed"], record["resources_indexed"], record["base_edition"] = _parse_fsnb_zip(
                    raw, db, record["source_url"], record["file_name"],
                )
        _rebuild_fts(db)
        db.commit()
        db.execute("INSERT INTO norms_fts(norms_fts) VALUES('optimize')")
        db.execute("INSERT INTO resources_fts(resources_fts) VALUES('optimize')")
        db.execute("INSERT INTO norm_resources_fts(norm_resources_fts) VALUES('optimize')")
        db.commit()
        db.close()
        db_temp.replace(CACHE_DIR / "index.sqlite")
    except Exception:
        db.close()
        db_temp.unlink(missing_ok=True)
        raise
    manifest["reindexed_at"] = retrieved_at()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+", default=list(DEFAULT_IDS), help="OpenData identifiers to download")
    parser.add_argument("--reindex", action="store_true", help="Rebuild the local index from existing cache files without network access")
    args = parser.parse_args()
    try:
        result = reindex_cached(tuple(args.ids)) if args.reindex else synchronize(tuple(args.ids))
    except (PortalError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"Ошибка синхронизации: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
