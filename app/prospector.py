from __future__ import annotations

import json
import os
import threading
import time
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
    "rejected": 0,
    "provider": None,
    "events": [],
}
_last_nominatim_request = 0.0

_OVERPASS_PROVIDERS = (
    ("Overpass DE", "https://overpass-api.de/api/interpreter"),
    ("Overpass Kumi", "https://overpass.kumi.systems/api/interpreter"),
)

_SEGMENT_TAGS = {
    "academia": [("leisure", "fitness_centre"), ("sport", "fitness"), ("leisure", "sports_centre")],
    "academias": [("leisure", "fitness_centre"), ("sport", "fitness"), ("leisure", "sports_centre")],
    "barbearia": [("shop", "hairdresser")],
    "salao": [("shop", "beauty"), ("shop", "hairdresser")],
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

_KEYWORDS = {
    "academia": ("academia", "fitness", "gym", "crossfit"),
    "academias": ("academia", "fitness", "gym", "crossfit"),
}

# Categorias confirmadas/documentadas no Geoapify para os primeiros segmentos.
# Segmentos ainda sem mapeamento continuam usando as fontes OSM de fallback.
_GEOAPIFY_CATEGORIES = {
    "academia": "sport.fitness",
    "academias": "sport.fitness",
    "restaurante": "catering.restaurant",
    "lanchonete": "catering.fast_food",
    "pizzaria": "catering.restaurant",
    "supermercado": "commercial.supermarket",
    "mercado": "commercial.supermarket",
    "hotel": "accommodation.hotel",
}

_NOMINATIM_SPECIAL = {
    "academia": ("fitness centre", "gym"),
    "academias": ("fitness centre", "gym"),
    "restaurante": ("restaurant",),
    "lanchonete": ("fast food",),
    "pizzaria": ("restaurant",),
    "barbearia": ("hairdresser",),
    "salao": ("beauty", "hairdresser"),
    "clinica": ("clinic",),
    "dentista": ("dentist",),
    "mercado": ("supermarket",),
    "supermercado": ("supermarket",),
    "farmacia": ("pharmacy",),
    "hotel": ("hotel",),
}


def _event(title: str, detail: str) -> None:
    event = {"title": title, "detail": detail, "time": datetime.now().strftime("%H:%M:%S")}
    with _lock:
        _state["activity"] = detail
        _state["events"] = ([event] + _state["events"])[:60]


def get_state() -> dict:
    with _lock:
        return {key: (list(value) if key == "events" else value) for key, value in _state.items()}


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value).strip().lower())
    return "".join(char for char in value if not unicodedata.combining(char))


def _request_json(url: str, *, data: bytes | None = None, timeout: int = 25) -> object:
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "EVO-Sales/1.0 local-prospecting-agent",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _nominatim(params: dict, timeout: int = 18) -> object:
    global _last_nominatim_request
    wait = 1.05 - (time.monotonic() - _last_nominatim_request)
    if wait > 0:
        time.sleep(wait)
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    result = _request_json(url, timeout=timeout)
    _last_nominatim_request = time.monotonic()
    return result


def _geocode_city(city: str) -> tuple[float, float, float, float] | None:
    result = _nominatim(
        {
            "q": f"{city}, Brasil",
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "br",
        }
    )
    if not isinstance(result, list) or not result or len(result[0].get("boundingbox") or []) != 4:
        return None
    south, north, west, east = map(float, result[0]["boundingbox"])
    return south, west, north, east


def _build_query(segment: str, bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    box = f"({south},{west},{north},{east})"
    selectors: list[str] = []
    for key, value in _SEGMENT_TAGS.get(_normalize(segment), []):
        for kind in ("node", "way", "relation"):
            selectors.append(f'{kind}["{key}"="{value}"]{box};')
    for keyword in _KEYWORDS.get(_normalize(segment), (_normalize(segment),)):
        safe = keyword.replace('"', '\\"')
        for kind in ("node", "way", "relation"):
            selectors.append(f'{kind}["name"~"{safe}",i]{box};')
    return "[out:json][timeout:25];(" + "".join(selectors) + ");out center tags;"


def _relevant(item: dict, segment: str) -> bool:
    tags = item.get("tags") or {}
    for key, value in _SEGMENT_TAGS.get(_normalize(segment), []):
        if tags.get(key) == value and (key, value) != ("leisure", "sports_centre"):
            return True
    text = _normalize(
        " ".join(
            str(tags.get(k, ""))
            for k in ("name", "description", "brand", "operator", "sport", "category", "type", "categories")
        )
    )
    return any(word in text for word in _KEYWORDS.get(_normalize(segment), (_normalize(segment),)))


def _geoapify_contact(properties: dict) -> str:
    datasource = properties.get("datasource") or {}
    raw = datasource.get("raw") or {} if isinstance(datasource, dict) else {}
    candidates = (
        properties.get("phone"),
        properties.get("website"),
        properties.get("email"),
        raw.get("contact:whatsapp") if isinstance(raw, dict) else None,
        raw.get("contact:phone") if isinstance(raw, dict) else None,
        raw.get("phone") if isinstance(raw, dict) else None,
        raw.get("contact:website") if isinstance(raw, dict) else None,
        raw.get("website") if isinstance(raw, dict) else None,
        raw.get("contact:email") if isinstance(raw, dict) else None,
        raw.get("email") if isinstance(raw, dict) else None,
    )
    for value in candidates:
        if value:
            return str(value).strip()[:120]
    return ""


def _query_geoapify(segment: str, bbox: tuple[float, float, float, float], limit: int) -> list[dict]:
    api_key = os.getenv("GEOAPIFY_API_KEY", "").strip()
    category = _GEOAPIFY_CATEGORIES.get(_normalize(segment))
    if not api_key:
        _event("Geoapify não configurado", "Defina GEOAPIFY_API_KEY para ativar o motor comercial.")
        return []
    if not category:
        _event("Categoria não mapeada", f"Geoapify ainda não tem mapeamento interno para '{segment}'. Usando fallback.")
        return []

    south, west, north, east = bbox
    params = urllib.parse.urlencode(
        {
            "categories": category,
            "filter": f"rect:{west},{north},{east},{south}",
            "limit": max(1, min(limit * 3, 30)),
            "lang": "pt",
            "apiKey": api_key,
        }
    )
    url = f"https://api.geoapify.com/v2/places?{params}"
    _event("Motor comercial", f"Consultando Geoapify Places para categoria {category}...")
    try:
        payload = _request_json(url, timeout=25)
    except Exception as exc:
        _event("Geoapify indisponível", f"A consulta falhou ({type(exc).__name__}). Usando fontes gratuitas de fallback.")
        return []

    features = payload.get("features", []) if isinstance(payload, dict) else []
    results: list[dict] = []
    seen: set[str] = set()
    for feature in features:
        properties = feature.get("properties") or {}
        name = str(properties.get("name") or "").strip()
        key = _normalize(name)
        if not name or key in seen:
            continue
        seen.add(key)
        categories = properties.get("categories") or []
        tags = {
            "name": name,
            "categories": " ".join(categories) if isinstance(categories, list) else str(categories),
            "category": category,
            "address": properties.get("formatted") or properties.get("address_line2") or "",
            "phone": _geoapify_contact(properties),
        }
        candidate = {"tags": tags, "provider": "Geoapify Places"}
        if _relevant(candidate, segment):
            results.append(candidate)
            _event("Empresa encontrada", f"{name} encontrada pelo Geoapify. Validando dados...")
        if len(results) >= limit:
            break

    if results:
        with _lock:
            _state["provider"] = "Geoapify Places"
        _event("Motor comercial ativo", f"Geoapify retornou {len(results)} empresa(s) compatível(is).")
    else:
        _event("Geoapify sem resultado", "Nenhuma empresa compatível foi retornada nessa área. Usando fallback.")
    return results


def _query_overpass(query: str) -> dict | None:
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    for provider_name, endpoint in _OVERPASS_PROVIDERS:
        _event("Consultando fallback", f"Tentando {provider_name}...")
        try:
            result = _request_json(endpoint, data=payload, timeout=35)
            if isinstance(result, dict):
                with _lock:
                    _state["provider"] = provider_name
                _event("Fallback respondeu", f"{provider_name} respondeu. Analisando resultados...")
                return result
        except Exception:
            _event("Fallback indisponível", f"{provider_name} falhou. Tentando a próxima fonte...")
    return None


def _bounded_nominatim(segment: str, bbox: tuple[float, float, float, float], limit: int) -> list[dict]:
    south, west, north, east = bbox
    viewbox = f"{west},{north},{east},{south}"
    results: list[dict] = []
    seen: set[str] = set()
    _event("Busca POI fallback", "Consultando pontos comerciais gratuitos dentro da cidade...")
    for phrase in _NOMINATIM_SPECIAL.get(_normalize(segment), (_normalize(segment),)):
        try:
            response = _nominatim(
                {
                    "q": f"[{phrase}]",
                    "format": "jsonv2",
                    "limit": min(limit, 10),
                    "countrycodes": "br",
                    "viewbox": viewbox,
                    "bounded": 1,
                    "extratags": 1,
                    "addressdetails": 1,
                }
            )
        except Exception:
            continue
        if not isinstance(response, list):
            continue
        for item in response:
            name = str(item.get("name") or str(item.get("display_name", "")).split(",")[0]).strip()
            key = _normalize(name)
            if not name or key in seen:
                continue
            tags = dict(item.get("extratags") or {})
            tags.update({"name": name, "category": item.get("category", ""), "type": item.get("type", "")})
            candidate = {"tags": tags, "provider": "OpenStreetMap/Nominatim"}
            if _relevant(candidate, segment):
                results.append(candidate)
                seen.add(key)
            if len(results) >= limit:
                return results
    return results


def _search(segment: str, city: str, limit: int) -> list[dict]:
    bbox = _geocode_city(city)
    if bbox is None:
        raise ValueError("cidade")
    _event("Cidade localizada", f"Área de {city} identificada. Iniciando descoberta comercial...")

    accepted: list[dict] = []
    seen: set[str] = set()

    # 1) Fonte principal: Geoapify Places, quando a chave estiver configurada.
    for candidate in _query_geoapify(segment, bbox, limit):
        name = _normalize((candidate.get("tags") or {}).get("name", ""))
        if name and name not in seen:
            accepted.append(candidate)
            seen.add(name)
        if len(accepted) >= limit:
            return accepted

    # 2) Fallback gratuito: Overpass.
    result = _query_overpass(_build_query(segment, bbox))
    elements = result.get("elements", []) if isinstance(result, dict) else []
    for item in elements:
        tags = item.get("tags") or {}
        name = str(tags.get("name", "")).strip()
        key = _normalize(name)
        if not name or key in seen:
            continue
        seen.add(key)
        item["provider"] = "OpenStreetMap/Overpass"
        if _relevant(item, segment):
            accepted.append(item)
        else:
            with _lock:
                _state["rejected"] += 1
            _event("Resultado descartado", f"{name} não corresponde ao segmento '{segment}'.")
        if len(accepted) >= limit:
            return accepted

    # 3) Último fallback: Nominatim limitado à cidade.
    if len(accepted) < limit:
        for candidate in _bounded_nominatim(segment, bbox, limit - len(accepted)):
            key = _normalize((candidate.get("tags") or {}).get("name", ""))
            if key and key not in seen:
                accepted.append(candidate)
                seen.add(key)
            if len(accepted) >= limit:
                break
    return accepted


def _contact(tags: dict) -> str:
    for key in (
        "contact:whatsapp",
        "whatsapp",
        "contact:phone",
        "phone",
        "contact:email",
        "email",
        "contact:website",
        "website",
        "url",
    ):
        if tags.get(key):
            return str(tags[key]).strip()[:120]
    return ""


def _exists(name: str, city: str) -> bool:
    with get_connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM leads WHERE lower(company_name)=lower(?) AND lower(city)=lower(?) LIMIT 1",
                (name.strip(), city.strip()),
            ).fetchone()
            is not None
        )


def run_mission(segment: str, city: str, limit: int = 5) -> None:
    segment, city, limit = segment.strip(), city.strip(), max(1, min(int(limit), 10))
    with _lock:
        if _state["running"]:
            return
        _state.update({"running": True, "found": 0, "saved": 0, "rejected": 0, "provider": None})
    try:
        _event("Missão iniciada", f"Pesquisando {segment} em {city}...")
        places = _search(segment, city, limit)
        with _lock:
            _state["found"] = len(places)
        if not places:
            _event("Pesquisa concluída", "Nenhuma fonte retornou empresa confiável para essa busca.")
            return

        for place in places:
            tags = place.get("tags") or {}
            name = str(tags.get("name", "")).strip()
            if not name or _exists(name, city):
                continue
            contact = _contact(tags)
            provider = str(place.get("provider") or _state.get("provider") or "EVO discovery")
            _event(
                "Lead validado",
                f"{name}: segmento confirmado" + (" e contato público encontrado." if contact else "; contato público ainda não disponível."),
            )
            saved = create_lead(
                LeadCreate(
                    company_name=name,
                    segment=segment,
                    city=city,
                    contact=contact,
                    source=provider,
                )
            )
            with _lock:
                _state["saved"] += 1
            _event("Oportunidade adicionada", f"{name}: score {saved['score']}/100, status {saved['status']}.")

        with _lock:
            saved_count, rejected_count = _state["saved"], _state["rejected"]
        _event("Missão concluída", f"{saved_count} lead(s) novo(s); {rejected_count} resultado(s) descartado(s).")
    except ValueError:
        _event("Cidade não localizada", f"Não consegui localizar '{city}'. Use Cidade, UF.")
    except Exception as exc:
        _event("Missão interrompida", f"Falha inesperada ({type(exc).__name__}), mas o servidor continua online.")
    finally:
        with _lock:
            _state["running"] = False
            _state["last_run"] = datetime.now(timezone.utc).isoformat()
