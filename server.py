#!/usr/bin/env python3
"""MCP server for FGIS CS public datasets and current price/index lookups."""
import json
import os
import statistics
from collections import defaultdict

from mcp.server import MCPServer
from local_index import find_coefficient_references, search_norms as query_norms
from local_index import search_resources as query_resources
from local_index import sync_status
from portal import (
    PORTAL_BASE,
    PortalError,
    get_json,
    list_open_datasets as fetch_open_datasets,
    resolve_price_filters,
    retrieved_at,
    search_public_indices,
    search_public_prices,
)

mcp = MCPServer("fgiscs-history", log_level=os.environ.get("LOG_LEVEL", "INFO").upper())

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SOURCE = "ФГИС ЦС (fgiscs.minstroyrf.ru), открытые данные Минстроя России"


def _load_jsonl(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _load_json(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return json.load(fh)


DATASETS = _load_json("datasets.json")
VERSIONS = _load_jsonl("versions.jsonl")
SALARY = _load_jsonl("oplata-truda.jsonl")


@mcp.tool()
async def list_datasets() -> str:
    """Перечислить наборы открытых данных ФГИС ЦС и глубину истории по каждому.

    Возвращает название набора, число сохранённых версий, период, за который
    доступна история, и число различных схем данных за этот период.
    """
    lines = [f"Источник: {SOURCE}", f"Наборов: {len(DATASETS)}", ""]
    for d in DATASETS:
        lines.append(
            f"{d['dataset']}\n"
            f"  {d['title']}\n"
            f"  версий: {d['versions']}, период: {d['first']}–{d['last']}, "
            f"схем: {len(d['structures'])}, объём выгрузок: {d['bytes'] / 1048576:.1f} МБ")
    return "\n".join(lines)


@mcp.tool()
async def dataset_versions(dataset: str) -> str:
    """История версий одного набора: когда публиковалась и менялось ли содержимое.

    Главное здесь — отметка о версиях, содержимое которых совпадает с другой версией:
    портал публикует обновление, но данные при этом не менялись. По самому порталу
    этого не видно — там указана только дата публикации.

    Args:
        dataset: идентификатор набора, например 7707082071-ksrms (см. list_datasets)
    """
    rows = [v for v in VERSIONS if v["dataset"] == dataset]
    if not rows:
        known = ", ".join(sorted({v["dataset"] for v in VERSIONS}))
        return f"Набор {dataset!r} не найден. Доступны: {known}"

    rows.sort(key=lambda v: v["data_date"] or "")
    title = rows[0]["dataset_title"]
    lines = [f"{title} ({dataset}) — {len(rows)} версий", ""]

    prev_struct = None
    for v in rows:
        marks = []
        if v["struct_date"] != prev_struct:
            if prev_struct is not None:
                marks.append(f"СХЕМА ИЗМЕНИЛАСЬ на {v['struct_date']}")
            prev_struct = v["struct_date"]
        if v["duplicate_of_another_version"]:
            marks.append("СОДЕРЖИМОЕ НЕ ИЗМЕНИЛОСЬ (побайтно совпадает с другой версией)")
        tail = ("  <- " + "; ".join(marks)) if marks else ""
        lines.append(f"  {v['data_date']}  {v['bytes'] / 1048576:8.2f} МБ  {v['sha256'][:12]}{tail}")
    return "\n".join(lines)


@mcp.tool()
async def salary_history(subject: str) -> str:
    """Динамика среднемесячной оплаты труда рабочего 1 разряда в строительстве по региону.

    Показывает ставку по каждой версии данных с 2020 года. В источнике лежит только
    последняя выгрузка — динамики там нет.

    Args:
        subject: название или часть названия субъекта РФ, например «Иркутская» или «Москва»
    """
    needle = subject.strip().lower()
    matched = [r for r in SALARY if needle in r["subject"].lower()]
    if not matched:
        return (f"Регион {subject!r} не найден. Попробуйте часть названия, "
                f"например «Иркутская», «Москва», «Татарстан».")

    by_code = defaultdict(list)
    for r in matched:
        by_code[(r["subject_code"], r["subject"])].append(r)

    lines = []
    for (code, name), rows in sorted(by_code.items()):
        rows.sort(key=lambda r: r["snapshot_date"])
        lines.append(f"{name} (код {code})")
        for r in rows:
            spread = ""
            if r["zones"] > 1:
                spread = (f"  (по {r['zones']} ценовым зонам: "
                          f"{r['rate_min']:,.0f}–{r['rate_max']:,.0f})")
            lines.append(f"  {r['snapshot_date']}  {r['rate_median']:>12,.2f} ₽{spread}")
        if len(rows) > 1:
            growth = (rows[-1]["rate_median"] / rows[0]["rate_median"] - 1) * 100
            lines.append(f"  итого за период: {growth:+.1f}%")
        lines.append("")
    lines.append(f"Источник: {SOURCE}. Единица — рубли в месяц, ставка рабочего 1 разряда.")
    return "\n".join(lines)


@mcp.tool()
async def salary_growth_ranking(limit: int = 10, ascending: bool = False) -> str:
    """Рейтинг регионов по росту оплаты труда в строительстве за всю доступную историю.

    Args:
        limit: сколько регионов показать
        ascending: True — начиная с наименьшего роста, False — с наибольшего
    """
    by_code = defaultdict(dict)
    for r in SALARY:
        by_code[r["subject_code"]][r["snapshot_date"]] = r

    dates = sorted({r["snapshot_date"] for r in SALARY})
    first, last = dates[0], dates[-1]

    ranked = []
    for series in by_code.values():
        if first in series and last in series:
            a, b = series[first]["rate_median"], series[last]["rate_median"]
            ranked.append(((b / a - 1) * 100, series[last]["subject"], a, b))
    ranked.sort(reverse=not ascending)

    limit = max(1, min(int(limit), len(ranked)))
    med = statistics.median(g for g, *_ in ranked)
    lines = [
        f"Рост среднемесячной оплаты труда рабочего 1 разряда, {first} → {last}",
        f"Регионов со сквозной историей: {len(ranked)}, медианный рост: {med:+.1f}%",
        "",
    ]
    for growth, name, a, b in ranked[:limit]:
        lines.append(f"  {growth:+7.1f}%  {name[:38]:38} {a:>12,.0f} → {b:>12,.0f} ₽")
    lines.append("")
    lines.append(f"Источник: {SOURCE}.")
    return "\n".join(lines)


@mcp.tool()
async def current_data_status() -> str:
    """Показать локальные выгрузки и их свежесть: дата публикации, загрузки и хеш.

    Снимки создаются командой `python sync_data.py`; этот инструмент ничего не загружает.
    """
    return sync_status()


@mcp.tool()
async def current_open_datasets() -> str:
    """Получить актуальный публичный каталог наборов ФГИС ЦС напрямую с портала."""
    try:
        rows = fetch_open_datasets()
    except PortalError as exc:
        return str(exc)
    if not rows:
        return "Публичный каталог ФГИС ЦС вернул пустой список."
    lines = [f"Каталог ФГИС ЦС на {retrieved_at()}", f"Источник: {PORTAL_BASE}/opendata", f"Наборов: {len(rows)}", ""]
    for row in rows:
        identifier = str(row.get("identificationNumber", "")).strip()
        lines.append(
            f"{row.get('datasetName', 'Без названия')} ({identifier}) — "
            f"обновлён {row.get('lastChangeDate', 'дата не указана')}; "
            f"карточка: {PORTAL_BASE}/opendata/{identifier}"
        )
    return "\n".join(lines)


@mcp.tool()
async def search_construction_resources(query: str, kind: str = "all", limit: int = 10) -> str:
    """Искать ресурсы по коду или названию в локальном индексе актуальных выгрузок.

    kind: all, materials, machines или labor. Для первого запуска выполните `python sync_data.py`.
    """
    try:
        rows = query_resources(query, kind, limit)
    except (FileNotFoundError, ValueError, OSError) as exc:
        return str(exc)
    if not rows:
        return f"Ресурс {query!r} не найден в текущем локальном индексе.\n{sync_status()}"
    lines = [f"Ресурсы по запросу {query!r}; источник — актуальные выгрузки ФГИС ЦС:"]
    for row in rows:
        lines.append(
            f"{row['code']} — {row['name']}" + (f"; ед. изм.: {row['unit']}" if row["unit"] else "")
            + f"; тип: {row['kind']}; набор: {row['source_name']}; источник: {row['source_url']}"
        )
    lines.append(sync_status())
    return "\n".join(lines)


@mcp.tool()
async def search_norms(query: str = "", norm_code: str = "", limit: int = 10) -> str:
    """Искать нормы ФСНБ по коду, описанию или составу ресурсов.

    Возвращает единицу измерения, технологический состав, ресурсы, базу, редакцию и первоисточник.
    Локальный индекс ФСНБ-2022 строится командой `python sync_data.py`.
    """
    try:
        rows = query_norms(query, limit, norm_code or None)
    except (FileNotFoundError, ValueError, OSError) as exc:
        return str(exc)
    if not rows:
        return f"Сметная норма не найдена по запросу {query or norm_code!r}.\n{sync_status()}"
    lines = [f"Найдено норм: {len(rows)}. Состав — по файлу ФСНБ; цены в ответе не рассчитываются."]
    for row in rows:
        lines.append(
            f"\n{row['code']} — {row['name']}\n"
            f"База: {row['base_type']} / {row['base_name']}; уровень цен: {row['price_level']}; "
            f"редакция файла: {row['creation_date']}; единица: {row['unit']}\n"
            f"Состав работ: {'; '.join(row['contents']) or 'не указан'}\n"
            f"Ресурсы: {json.dumps(row['resources'], ensure_ascii=False)}\n"
            f"Нормативные ссылки из строки нормы: {json.dumps(row['normative_basis'], ensure_ascii=False)}\n"
            f"Исходный файл: {row['source_url']}"
        )
    return "\n".join(lines)


@mcp.tool()
async def search_current_prices(
    query: str,
    subject: str,
    period: str = "",
    price_zone: str = "",
    authority: str = "",
    kind: str = "materials",
    limit: int = 10,
) -> str:
    """Запросить текущие цены материалов/машин из публичного API ФГИС ЦС.

    Если период не задан, используется самый новый период портала. Если регион имеет
    несколько ценовых зон, укажите зону; неоднозначные значения будут перечислены.
    """
    if not query.strip() or not subject.strip():
        return "Нужно указать поисковый запрос и субъект РФ."
    try:
        filters = resolve_price_filters(subject, period or None, price_zone or None, authority or None)
        rows = search_public_prices(query, filters, kind, limit)
    except (PortalError, ValueError, TypeError) as exc:
        return str(exc)
    result = {
        "source": f"{PORTAL_BASE}/prices",
        "retrieved_at": retrieved_at(),
        "subject": filters["subject"]["name"],
        "price_zone": filters["zone"]["name"],
        "period": filters["period"]["name"],
        "authority": filters["authority"]["name"] if filters.get("authority") else "все доступные организации",
        "kind": kind,
        "query": query,
        "items": rows,
    }
    if not rows:
        return "По заданным фильтрам портал не вернул опубликованных цен.\n" + json.dumps(result, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def search_current_indices(
    query: str,
    subject: str,
    period: str = "",
    price_zone: str = "",
    authority: str = "",
    index_type: str = "resource_groups",
    limit: int = 10,
) -> str:
    """Запросить опубликованные индексы из публичных разделов ФГИС ЦС.

    Сейчас поддерживаются индексы к группам однородных строительных ресурсов.
    Период не указан — будет выбран новейший доступный квартал.
    """
    if not query.strip() or not subject.strip():
        return "Нужно указать поисковый запрос и субъект РФ."
    try:
        filters = resolve_price_filters(subject, period or None, price_zone or None, authority or None)
        rows = search_public_indices(query, filters, index_type, limit)
    except (PortalError, ValueError, TypeError) as exc:
        return str(exc)
    result = {
        "source": f"{PORTAL_BASE}/prices",
        "retrieved_at": retrieved_at(),
        "subject": filters["subject"]["name"],
        "price_zone": filters["zone"]["name"],
        "period": filters["period"]["name"],
        "authority": filters["authority"]["name"] if filters.get("authority") else "все доступные организации",
        "index_type": index_type,
        "query": query,
        "field_meanings": {
            "price": "Сметная цена группы в уровне цен на 01.01.2022 без оплаты труда машинистов, руб.",
            "index": "Индекс изменения сметной стоимости к группе однородных строительных ресурсов.",
        },
        "coverage_note": "Это индекс к группе однородных ресурсов; он не заменяет индексы по видам затрат.",
        "items": rows,
    }
    if not rows:
        return "Публичный API не вернул подходящих строк индексов для этих фильтров.\n" + json.dumps(result, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def search_coefficient_references(query: str = "", norm_code: str = "", limit: int = 10) -> str:
    """Искать привязанные к норме ссылки на основания в ФСНБ.

    Текущая выгрузка ФСНБ не является каталогом коэффициентов по условиям работ и
    не содержит для них нормализованных значений. Этот инструмент возвращает только
    ссылки из полей нормы и явно не выдаёт неподтверждённый коэффициент.
    """
    try:
        rows = find_coefficient_references(query, norm_code or None, limit)
    except (FileNotFoundError, ValueError, OSError) as exc:
        return str(exc)
    if not rows:
        return (
            "В проиндексированных данных нет подтверждённого справочника коэффициентов "
            "по условиям работ для этого запроса. Значение нельзя надёжно вывести из одной нормы.\n"
            "Индексирует актуальный файл ФСНБ; нормативное основание должно быть проверено по документу-первоисточнику.\n"
            + sync_status()
        )
    return (
        "Найдены только нормативные ссылки, прикреплённые к норме. Они сами по себе не являются "
        "значениями коэффициентов и не подтверждают условие применения.\n"
        + json.dumps(rows, ensure_ascii=False, indent=2)
    )


def main():
    """Entry point for the `fgiscs-history-mcp` console script."""
    transport = os.environ.get("FGISCS_MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=os.environ.get("FGISCS_MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("FGISCS_MCP_PORT", "8000")),
            streamable_http_path=os.environ.get("FGISCS_MCP_PATH", "/mcp"),
        )
        return
    raise ValueError("FGISCS_MCP_TRANSPORT должен быть stdio или streamable-http.")


if __name__ == "__main__":
    main()
