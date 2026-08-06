#!/usr/bin/env python3
"""MCP server over the version history of Russian construction-pricing open data.

The source portal (ФГИС ЦС, fgiscs.minstroyrf.ru) publishes only the latest export
of each dataset. This server serves what the portal does not: the history — what
changed, when, and what did not change despite being republished.

Data ships with the server as plain JSONL files under data/. No network calls,
no API key, no account.
"""
import json
import os
import statistics
from collections import defaultdict

from mcp.server import MCPServer

mcp = MCPServer("fgiscs-history")

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


def main():
    """Entry point for the `fgiscs-history-mcp` console script."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
