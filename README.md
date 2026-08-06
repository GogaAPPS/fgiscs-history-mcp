# fgiscs-history-mcp

MCP server over the **version history** of Russian construction-pricing open data
(ФГИС ЦС — the Federal State Information System for Construction Pricing, operated by
Glavgosekspertiza of Russia under Government Decree № 1452 of 23.12.2016).

The source portal publishes only the **latest** export of each dataset. This server
serves what the portal does not: what changed, when — and what did **not** change
despite being republished.

History covers **198 versions of 15 datasets, from 2017 to 2026**.

## Why this exists

Open data portals are built for downloading, not for comparing. If you want to know how
a value moved over six years, you have to find every past export, unpack it, repair it
and stitch it together. That work is done here once, so it does not have to be done again.

A concrete example the server can answer and the portal cannot: two exports of the
construction resources classifier — `2018-11-22` and `2019-06-17` — are **byte-identical**.
The portal published an update in which nothing had changed. A user of the portal sees
only the publication date and concludes, wrongly, that the data moved.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/elysosss/fgiscs-history-mcp
cd fgiscs-history-mcp
pip install -e .
```

Add to your MCP client config (Claude Desktop, Claude Code, Cursor, …):

```json
{
  "mcpServers": {
    "fgiscs-history": {
      "command": "python",
      "args": ["/absolute/path/to/fgiscs-history-mcp/server.py"]
    }
  }
}
```

No API key, no account, no network calls — the data ships with the server (240 KB).

## Tools

| Tool | What it answers |
|---|---|
| `list_datasets` | Which datasets exist and how deep the history goes for each |
| `dataset_versions` | Every version of one dataset, flagging schema changes and republished-but-unchanged exports |
| `salary_history` | Monthly wage rate for a grade-1 construction worker in a given region, 2020 → 2026 |
| `salary_growth_ranking` | Regions ranked by wage growth over the full period |

Example questions your assistant can now answer:

- «Как менялась ставка рабочего 1 разряда в Иркутской области с 2020 года?»
  → +202.5 % (41 621 → 125 911 ₽), with the value for each of the 8 published versions.
- «В каких регионах оплата труда росла медленнее всего?»
  → Ненецкий АО +44.7 %, Республика Коми +51.1 %, median across 74 regions +88.9 %.
- «Сколько раз менялась схема Классификатора строительных ресурсов?»
  → once, on 2022-11-17, across 41 versions.

## Data

| File | Contents |
|---|---|
| `data/datasets.json` | 15 datasets: version count, period covered, schema count, export size |
| `data/versions.jsonl` | 198 versions: date, schema date, size, sha256, duplicate flag, source URL |
| `data/oplata-truda.jsonl` | 662 rows — wage series by region across 8 versions |

Every value carries its source: each version record links back to the original file on
`fgiscs.minstroyrf.ru`. Nothing here is scraped — the portal's open-data API is public
and anonymous, and the ingestion pipeline respects it with pauses and backoff.

### Known caveats

- Region names in the source exports contain latin look-alike letters inside Russian
  words (`Республика Caxa`, `Чукотский автономный oКруг`), inconsistent spellings across
  versions, and renames. Series are therefore keyed by **region code**, not by name.
- Price-zone slicing changes between versions (91 zone rows in 2020, 117 in 2026), so the
  per-region figure is a **median across that region's zones**; min and max are kept.
- 11 of 85 regions lack a start-to-end series: most appeared in the data after 2020.

## Licence

MIT. The code is free to copy. The data is public open data, and its history is what
took the work.

Russian: [`docs/ru/README.md`](docs/ru/README.md)
