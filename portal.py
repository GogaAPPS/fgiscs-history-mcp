"""Small, read-only client for the public endpoints used by the FGIS CS portal."""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import truststore


API_BASE = os.environ.get("FGISCS_API_BASE", "https://fgiscs.minstroyrf.ru/api/").rstrip("/") + "/"
PORTAL_BASE = API_BASE.removesuffix("/api/")
TIMEOUT = float(os.environ.get("FGISCS_TIMEOUT", "20"))
USER_AGENT = "fgiscs-history-mcp/0.2 (read-only open-data client)"
TLS_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class PortalError(RuntimeError):
    """A portal request failed or returned an unsupported response."""


def _open(path: str, params: dict[str, Any] | None = None):
    url = urllib.parse.urljoin(API_BASE, path.lstrip("/"))
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT, context=TLS_CONTEXT)
    except urllib.error.HTTPError as exc:
        raise PortalError(f"ФГИС ЦС вернула HTTP {exc.code} для {path}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PortalError(f"Не удалось подключиться к публичному API ФГИС ЦС: {exc}.") from exc


def get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    with _open(path, params) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise PortalError("Ответ API превысил допустимые 8 МБ.")
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortalError(f"API ФГИС ЦС вернул не JSON для {path}.") from exc


def get_bytes(path: str, max_bytes: int = 250 * 1024 * 1024) -> tuple[bytes, str | None]:
    """Download one public file with a hard size bound."""
    with _open(path) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise PortalError(f"Файл слишком велик ({int(length)} байт; лимит {max_bytes}).")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise PortalError(f"Файл превысил лимит {max_bytes} байт.")
        disposition = response.headers.get("Content-Disposition")
    return b"".join(chunks), disposition


def list_open_datasets() -> list[dict[str, Any]]:
    result = get_json("OpenData", {"page": 1, "take": 200}) or {}
    return result.get("items", []) if isinstance(result, dict) else []


def get_dataset_detail(identifier: str, listed_identifier: str | None = None) -> dict[str, Any]:
    # The current catalog contains a leading space in the FSNB-2022 identifier.
    # Preserve and URL-encode the exact value returned by the catalog in that case.
    candidates = [identifier.strip()]
    if listed_identifier and listed_identifier != identifier.strip():
        candidates.insert(0, listed_identifier)
    errors: list[str] = []
    for candidate in dict.fromkeys(candidates):
        path = "OpenData/GetByNumber/" + urllib.parse.quote(candidate, safe="-")
        try:
            result = get_json(path)
            if isinstance(result, dict) and result.get("datasetFile"):
                return result
            errors.append("карточка не содержит ссылки на выгрузку")
        except PortalError as exc:
            errors.append(str(exc))
    raise PortalError(f"Не удалось получить карточку набора {identifier}: {'; '.join(errors)}")


def file_content_url(file_id: str) -> str:
    return urllib.parse.urljoin(API_BASE, "values/GetFileContent/" + urllib.parse.quote(file_id, safe="-"))


def retrieved_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _select(items: list[dict[str, Any]], needle: str | None, label: str) -> dict[str, Any]:
    if not items:
        raise PortalError(f"ФГИС ЦС не вернула список: {label}.")
    if not needle:
        return items[0]
    folded = needle.strip().casefold()
    exact = [x for x in items if str(x.get("name", "")).strip().casefold() == folded]
    matches = exact or [x for x in items if folded in str(x.get("name", "")).casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        names = ", ".join(str(x.get("name", "")) for x in items[:20])
        raise PortalError(f"{label} {needle!r} не найден. Варианты: {names}")
    names = ", ".join(str(x.get("name", "")) for x in matches[:20])
    raise PortalError(f"{label} неоднозначен; уточните один из вариантов: {names}")


def resolve_price_filters(
    subject: str,
    period: str | None = None,
    price_zone: str | None = None,
    authority: str | None = None,
) -> dict[str, Any]:
    subjects = get_json("EstimatedPrice/CountrySubjects") or []
    selected_subject = _select(subjects, subject, "Субъект РФ")
    zones = get_json("EstimatedPrice/PriceZones", {"subjectId": selected_subject["id"]}) or []
    if not price_zone and len(zones) > 1:
        names = ", ".join(str(x.get("name", "")) for x in zones[:20])
        raise PortalError(f"Для субъекта {selected_subject['name']} несколько ценовых зон; укажите одну: {names}")
    selected_zone = _select(zones, price_zone, "Ценовая зона")
    periods = get_json("EstimatedPrice/Periods", {"priceZoneId": selected_zone["id"]}) or []
    selected_period = _select(periods, period, "Период")
    authorities = get_json(
        "EstimatedPrice/Authorities",
        {"priceZoneId": selected_zone["id"], "periodId": selected_period["id"]},
    ) or []
    selected_authority = _select(authorities, authority, "Отраслевая организация") if authority else None
    return {
        "subject": selected_subject,
        "zone": selected_zone,
        "period": selected_period,
        "authority": selected_authority,
        "authorities": authorities,
    }


def search_public_prices(
    query: str,
    filters: dict[str, Any],
    kind: str = "materials",
    limit: int = 10,
) -> Any:
    row_endpoint = {
        "materials": "EstimatedPrice/BuildingResources/Search/Materials",
        "machines": "EstimatedPrice/BuildingResources/Search/Machines",
    }.get(kind)
    if row_endpoint is None:
        raise PortalError("Тип цены должен быть materials или machines.")
    params: dict[str, Any] = {
        "subjectId": filters["subject"]["id"],
        "priceZoneId": filters["zone"]["id"],
        "periodId": filters["period"]["id"],
        "search": query,
        "page": 1,
        "take": max(1, min(int(limit), 25)),
    }
    if filters.get("authority"):
        params["authorityId"] = filters["authority"]["id"]
    suggestions = get_json("EstimatedPrice/BuildingResources/Search", {
        **params, "materials": kind == "materials", "machines": kind == "machines",
        "equipment": False, "value": query,
    }) or []
    if isinstance(suggestions, dict):
        suggestions = suggestions.get("items")
    if not isinstance(suggestions, list):
        raise PortalError("Публичный поиск цен вернул неподдерживаемую структуру.")
    rows: list[dict[str, Any]] = []
    for suggestion in suggestions[:params["take"]]:
        if not isinstance(suggestion, dict) or suggestion.get("id") is None:
            raise PortalError("Публичный поиск цен вернул вариант без идентификатора ресурса.")
        identifier = suggestion["id"]
        result = get_json(row_endpoint, {
            **params,
            "value": identifier,
            "search": suggestion.get("title", query),
            "materials": kind == "materials",
            "machines": kind == "machines",
        })
        if isinstance(result, list):
            if not all(isinstance(row, dict) for row in result):
                raise PortalError("Таблица цен вернула строки неподдерживаемого формата.")
            rows.extend(result)
        elif isinstance(result, dict) and isinstance(result.get("items"), list):
            # Price-table responses group resource rows inside top-level items:
            # {"items": [{"id": ..., "items": [{...}], "total": ...}]}
            for group in result["items"]:
                if not isinstance(group, dict):
                    raise PortalError("Таблица цен вернула группу неподдерживаемого формата.")
                group_rows = group.get("items")
                if isinstance(group_rows, list):
                    if not all(isinstance(row, dict) for row in group_rows):
                        raise PortalError("Таблица цен вернула строки неподдерживаемого формата.")
                    rows.extend(group_rows)
                elif any(key in group for key in ("code", "name", "estimatedPrice", "aggregatedPrice")):
                    # Also accept a flat items list in case the portal changes
                    # its response shape between API versions.
                    rows.append(group)
                else:
                    raise PortalError("Таблица цен вернула группу без списка строк.")
        else:
            raise PortalError("Таблица цен вернула неподдерживаемую структуру.")
        if len(rows) >= params["take"]:
            break
    return rows[:params["take"]]


def search_public_indices(
    query: str,
    filters: dict[str, Any],
    index_type: str = "resource_groups",
    limit: int = 10,
) -> Any:
    """Resolve portal search suggestions to concrete rows in the public index tree."""
    take = max(1, min(int(limit), 25))
    params: dict[str, Any] = {
        "subjectId": filters["subject"]["id"],
        "priceZoneId": filters["zone"]["id"],
        "periodId": filters["period"]["id"],
    }
    if filters.get("authority"):
        params["authorityId"] = filters["authority"]["id"]
    if index_type == "resource_groups":
        suggestions = get_json("IndicesForResourcesGroups/Search", {
            **params,
            "materials": True,
            "machines": True,
            "searchValue": query,
            "page": 1,
            "take": take,
        }) or []
        if not isinstance(suggestions, list):
            raise PortalError("Публичный поиск индексов вернул неподдерживаемую структуру.")
        rows: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for suggestion in suggestions[:take]:
            resource_id = suggestion.get("id") if isinstance(suggestion, dict) else None
            if resource_id is None:
                continue
            # The portal has separate material and machine trees, while its
            # autocomplete returns both kinds without a kind discriminator.
            for is_materials in (True, False):
                groups = get_json("IndicesForResourcesGroups/GetTreeViewResourceGroups", {
                    **params, "isMaterials": is_materials, "searchResourceId": resource_id,
                }) or []
                matched_rows: list[dict[str, Any]] = []
                for group in groups:
                    group_id = group.get("value") if isinstance(group, dict) else None
                    if group_id is None:
                        continue
                    group_rows = get_json("IndicesForResourcesGroups/GetTreeViewResourcesInGroup", {
                        **params, "groupId": group_id, "searchResourceId": resource_id,
                    }) or []
                    if isinstance(group_rows, list):
                        matched_rows.extend(row for row in group_rows if isinstance(row, dict))
                if matched_rows:
                    for row in matched_rows:
                        row_id = row.get("id")
                        if row_id is not None and row_id not in seen:
                            seen.add(row_id)
                            rows.append(row)
                            if len(rows) >= take:
                                return rows
                    break
        return rows
    raise PortalError("Пока поддерживаются только индексы к группам однородных строительных ресурсов.")


def _paged_table_rows(path: str, params: dict[str, Any], *, flatten_groups: bool = False) -> list[dict[str, Any]]:
    """Read every page of a public table, failing rather than returning a partial result."""
    take = 200
    rows: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(1, 51):
        result = get_json(path, {**params, "page": page, "take": take})
        if isinstance(result, list):
            page_items = result
            total = len(result)
        elif isinstance(result, dict) and isinstance(result.get("items"), list):
            page_items = result["items"]
            if result.get("total") is not None:
                total = int(result["total"])
        else:
            raise PortalError(f"Таблица {path} вернула неподдерживаемую структуру.")

        for item in page_items:
            if not isinstance(item, dict):
                raise PortalError(f"Таблица {path} содержит строку неподдерживаемого формата.")
            nested = item.get("items")
            if flatten_groups and isinstance(nested, list):
                for child in nested:
                    if not isinstance(child, dict):
                        raise PortalError(f"Таблица {path} содержит вложенную строку неподдерживаемого формата.")
                    rows.append({**item, **child, "items": None})
            else:
                rows.append(item)

        if total is None or len(page_items) < take or page * take >= total:
            return rows
    raise PortalError(f"Таблица {path} превышает лимит чтения; вернулась только часть данных.")


def _filter_rows(rows: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    needle = query.strip().casefold()
    if needle:
        rows = [row for row in rows if needle in json.dumps(row, ensure_ascii=False).casefold()]
    return rows[:max(1, min(int(limit), 25))]


def search_public_labor_prices(
    query: str,
    filters: dict[str, Any],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search the public RIM labor-price registry in rubles per person-hour."""
    params: dict[str, Any] = {
        "periodId": filters["period"]["id"],
        "priceZoneId": filters["zone"]["id"],
    }
    if filters.get("authority"):
        params["authorityId"] = filters["authority"]["id"]
    rows = _paged_table_rows("EstimatedPrice/RimWorkerSalaryRegistry", params)
    return _filter_rows(rows, query, limit)


def _select_option(options: list[Any], needle: str | None, label: str) -> Any:
    if not options:
        raise PortalError(f"ФГИС ЦС не вернула варианты для фильтра «{label}».")
    if not needle:
        if len(options) == 1:
            return options[0]
        names = ", ".join(str(item.get("name", item.get("label", item))) if isinstance(item, dict) else str(item) for item in options[:20])
        raise PortalError(f"Укажите фильтр «{label}». Доступны варианты: {names}")
    folded = needle.strip().casefold()
    labels = [str(item.get("name", item.get("label", item))) if isinstance(item, dict) else str(item) for item in options]
    exact = [i for i, name in enumerate(labels) if name.strip().casefold() == folded]
    matches = exact or [i for i, name in enumerate(labels) if folded in name.casefold()]
    if len(matches) == 1:
        return options[matches[0]]
    if not matches:
        raise PortalError(f"Значение фильтра «{label}» {needle!r} не найдено. Доступны варианты: {', '.join(labels[:20])}")
    raise PortalError(f"Значение фильтра «{label}» неоднозначно: {', '.join(labels[i] for i in matches[:20])}")


def search_public_transport_prices(
    query: str,
    filters: dict[str, Any],
    transport_type: str = "rail",
    road_type: str | None = None,
    vehicle_type: str | None = None,
    vehicle_load_capacity: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search the public road/rail freight and road loading price tables."""
    common: dict[str, Any] = {
        "periodId": filters["period"]["id"],
        "priceZoneId": filters["zone"]["id"],
    }
    if filters.get("authority"):
        common["authorityId"] = filters["authority"]["id"]

    if transport_type == "rail":
        rows = _paged_table_rows("EstimatedPrice/Services/TransportationByRail", common, flatten_groups=True)
    elif transport_type == "loading_auto":
        rows = _paged_table_rows("EstimatedPrice/Services/LoadWorksByAuto", common)
    elif transport_type == "auto":
        if not filters.get("authority"):
            names = ", ".join(str(item.get("name", "")) for item in filters.get("authorities", [])[:20])
            raise PortalError(f"Для автомобильной перевозки укажите отраслевую организацию. Варианты: {names or 'нет доступных'}")
        common["subjectId"] = filters["subject"]["id"]
        road_types = get_json("EstimatedPrice/Services/TransportationByAuto/RoadType", common) or []
        selected_road = _select_option(road_types, road_type, "тип дорог")
        road_value = selected_road.get("name", selected_road) if isinstance(selected_road, dict) else selected_road
        vehicle_types = get_json("EstimatedPrice/Services/TransportationByAuto/VehicleType", {
            **common, "roadType": road_value,
        }) or []
        selected_vehicle = _select_option(vehicle_types, vehicle_type, "тип автотранспортного средства")
        vehicle_value = selected_vehicle.get("name", selected_vehicle) if isinstance(selected_vehicle, dict) else selected_vehicle
        capacities = get_json("EstimatedPrice/Services/TransportationByAuto/VehicleLoadCapacity", {
            **common, "roadType": road_value, "vehicleType": vehicle_value,
        }) or []
        selected_capacity = _select_option(capacities, vehicle_load_capacity, "грузоподъёмность/объём барабана")
        capacity_value = selected_capacity.get("name", selected_capacity) if isinstance(selected_capacity, dict) else selected_capacity
        rows = _paged_table_rows("EstimatedPrice/Services/TransportationByAuto/Data", {
            **common, "roadType": road_value, "vehicleType": vehicle_value,
            "vehicleLoadCapacity": capacity_value,
        })
    else:
        raise PortalError("Тип услуги должен быть auto, loading_auto или rail.")
    return _filter_rows(rows, query, limit)


def search_public_building_indices(
    query: str,
    filters: dict[str, Any],
    index_type: str = "building_types",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search public construction-object or direct-cost-element index tables."""
    if index_type not in {"building_types", "direct_cost_elements"}:
        raise PortalError("Тип индекса должен быть building_types или direct_cost_elements.")
    params: dict[str, Any] = {
        "periodId": filters["period"]["id"],
        "priceZoneId": filters["zone"]["id"],
        "hasDirectCostElementType": index_type == "direct_cost_elements",
    }
    if filters.get("authority"):
        params["authorityId"] = filters["authority"]["id"]
    rows = _paged_table_rows("IndicesOfChangeEstimatedPrice", params)
    return _filter_rows(rows, query, limit)
