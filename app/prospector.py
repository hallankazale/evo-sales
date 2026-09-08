from __future__ import annotations

import json
import threading
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from app.database import get_connection
from app.schemas import LeadCreate
from app.services import create_lead

_lock = threading.Lock()
_state = {
    "running": False,
    "activity": "Aguardando missão",
    "last_run": None,
    "found": 0,
    "saved": 0,
    "events": [],
}

# Mapeia termos comuns em português para tags do OpenStreetMap.
_SEGMENT_TAGS = {
    "academia": [("leisure", "fitness_centre"), ("leisure", "sports_centre"), ("sport", "fitness")],
    "academias": [("leisure", "fitness_centre"), ("leisure", "sports_centre"), ("sport", "fitness")],
    "barbearia": [("shop", "hairdresser")],
    "salao": [("shop", "hairdresser"), ("shop", "beauty")],
    "salao de beleza": [("shop", "beauty"), ("shop", "hairdresser")],
    "restaurante": [("amenity", "restaurant")],
    "lanchonete": [("amenity", "fast_food")],
    "pizzaria": [("amenity", "restaurant")],
    "clinica": [("amenity", "clinic"), ("healthcare", "clinic")],
    "dentista": [("amenity", "dentist")],
    "mercado": [("shop", "supermarket"), ("shop", "convenience")],
    "supermercado": [("shop", "supermarket")],
    "oficina": [("shop", "car_repair")],
    "imobiliaria": [("office", "estate_agent")],
    "farmacia": [("amenity", "pharmacy")],
    "pet shop": [("shop", "pet")],
    "hotel": [("tourism", "hotel")],
}


def _event(title: str, detail: str) -> None:
    event = {
        "title": title,
        "detail": detail,
        "time": datetime.now().strftime("%H:%M:%S"),
    }
    with _lock:
        _state["activity"] = detail
        _state["events"] = ([event] + _state["events"])[:30]


def get_state() -> dict:
    with _lock:
        return {
            "running": _state["running"],
            "activity": _state["activity"],
            "last_run": _state["last_run"],
            "found": _state["found"],
            "saved": _state["saved"],
            "events": list(_state["events"]),
        }


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _request_json(url: str, *, data: bytes | None = None, timeout: int = 20) -> object:
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "EVO-Sales/0.4 local-prospecting",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _geocode_city(city: str) -> tuple[float, float, float, float] | None:
    params = urllib.parse.urlencode({
        "q": f"{city}, Brasil",
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "br",
        "addressdetails": 1,
    })
    results = _request_json(f"https://nominatim.openstreetmap.org/search?{params}")
    if not isinstance(results, list) or not results:
        return None

    bbox = results[0].get("boundingbox")
    if not bbox or len(bbox) != 4:
        return None

    south, north, west, east = map(float, bbox)
    return south, west, north, east


def _build_overpass_query(segment: str, bbox: tuple[float, float, float, float], limit: int) -> str:
    south, west, north, east = bbox
    box = f"({south},{west},{north},{east})"
    tags = _SEGMENT_TAGS.get(_normalize(segment))

    selectors: list[str] = []
    if tags:
        for key, value in tags:
            selectors.extend([
                f'node["{key}"="{value}"]{box};',
                f'way["{key}"="{value}"]{box};',
                f'relation["{key}"="{value}"]{box};',
            ])
    else:
        # Fallback seguro para segmentos ainda não mapeados: procura o termo no nome.
        escaped = segment.replace('"', '\\"')
        selectors.extend([
            f'node["name"~"{escaped}",i]{box};',
            f'way["name"~"{escaped}",i]{box};',
            f'relation["name"~"{escaped}",i]{box};',
        ])

    # O limit é aplicado após a resposta para não criar consultas Overpass inválidas.
    return "[out:json][timeout:20];(" + "".join(selectors) + ");out center tags;"


def _search_overpass(segment: str, city: str, limit: int) -> list[dict]:
    bbox = _geocode_city(city)
    if bbox is None:
        raise ValueError("cidade_nao_localizada")

    _event("Cidade localizada", f"Área de {city} identificada. Buscando estabelecimentos...")
    query = _build_overpass_query(segment, bbox, limit)
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    result = _request_json("https://overpass-api.de/api/interpreter", data=payload, timeout=30)
    if not isinstance(result, dict):
        return []

    elements = result.get("elements") or []
    valid = [item for item in elements if isinstance(item, dict) and (item.get("tags") or {}).get("name")]
    return valid[:limit]


def _extract_company(place: dict) -> str:
    return str((place.get("tags") or {}).get("name") or "").strip()


def _extract_contact(place: dict) -> str:
    tags = place.get("tags") or {}
    for key in (
        "contact:whatsapp",
        "contact:phone",
        "phone",
        "contact:email",
        "email",
        "contact:website",
        "website",
    ):
        value = tags.get(key)
        if value:
            return str(value).strip()[:120]
    return ""


def _already_exists(company_name: str, city: str) -> bool:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM leads WHERE lower(company_name) = lower(?) AND lower(city) = lower(?) LIMIT 1",
            (company_name.strip(), city.strip()),
        ).fetchone()
    return row is not None


def run_mission(segment: str, city: str, limit: int = 5) -> None:
    segment = segment.strip()
    city = city.strip()
    limit = max(1, min(int(limit), 10))

    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
        _state["found"] = 0
        _state["saved"] = 0

    try:
        _event("Missão iniciada", f"Pesquisando {segment} em {city}...")
        places = _search_overpass(segment, city, limit)
        with _lock:
            _state["found"] = len(places)

        if not places:
            _event(
                "Pesquisa concluída",
                "A cidade foi localizada, mas o OpenStreetMap não possui estabelecimentos desse segmento cadastrados nessa área.",
            )
            return

        for place in places:
            company = _extract_company(place)
            if not company or _already_exists(company, city):
                continue

            contact = _extract_contact(place)
            detail = "contato público localizado" if contact else "sem contato público cadastrado"
            _event("Empresa encontrada", f"Analisando {company} — {detail}...")

            lead = LeadCreate(
                company_name=company,
                segment=segment,
                city=city,
                contact=contact,
                source="OpenStreetMap/Overpass",
            )
            saved = create_lead(lead)
            with _lock:
                _state["saved"] += 1
            _event(
                "Oportunidade adicionada",
                f"{company}: score {saved['score']}/100. Abordagem preparada para aprovação.",
            )

        with _lock:
            saved_count = _state["saved"]
        if saved_count == 0:
            _event("Pesquisa concluída", "Os resultados encontrados já estavam cadastrados no pipeline.")
        else:
            _event("Missão concluída", f"Pesquisa finalizada. {saved_count} nova(s) oportunidade(s) adicionada(s).")
    except ValueError:
        _event("Cidade não localizada", f"Não consegui localizar '{city}'. Use no formato Cidade, UF.")
    except Exception as exc:
        _event("Falha na pesquisa", f"Não foi possível concluir a missão: {type(exc).__name__}.")
    finally:
        with _lock:
            _state["running"] = False
            _state["last_run"] = datetime.now(timezone.utc).isoformat()
