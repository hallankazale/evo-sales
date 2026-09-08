from __future__ import annotations

import json
import threading
import unicodedata
import urllib.error
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
    "academia": ("academia", "fitness", "gym", "musculacao", "crossfit", "treino"),
    "academias": ("academia", "fitness", "gym", "musculacao", "crossfit", "treino"),
}


def _event(title: str, detail: str) -> None:
    event = {"title": title, "detail": detail, "time": datetime.now().strftime("%H:%M:%S")}
    with _lock:
        _state["activity"] = detail
        _state["events"] = ([event] + _state["events"])[:50]


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
        headers={"User-Agent": "EVO-Sales/0.7 prospecting-agent", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _geocode_city(city: str) -> tuple[float, float, float, float] | None:
    params = urllib.parse.urlencode({
        "q": f"{city}, Brasil",
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "br",
    })
    result = _request_json(f"https://nominatim.openstreetmap.org/search?{params}", timeout=18)
    if not isinstance(result, list) or not result:
        return None
    bbox = result[0].get("boundingbox") or []
    if len(bbox) != 4:
        return None
    south, north, west, east = map(float, bbox)
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
    expected = _SEGMENT_TAGS.get(_normalize(segment), [])
    for key, value in expected:
        if tags.get(key) == value and (key, value) != ("leisure", "sports_centre"):
            return True
    text = _normalize(" ".join(str(tags.get(key, "")) for key in ("name", "description", "brand", "operator", "sport")))
    return any(word in text for word in _KEYWORDS.get(_normalize(segment), (_normalize(segment),)))


def _query_overpass(query: str) -> tuple[dict | None, str | None]:
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error: str | None = None
    for provider_name, endpoint in _OVERPASS_PROVIDERS:
        _event("Consultando fonte", f"Tentando {provider_name}...")
        try:
            result = _request_json(endpoint, data=payload, timeout=35)
            if isinstance(result, dict):
                with _lock:
                    _state["provider"] = provider_name
                _event("Fonte disponível", f"{provider_name} respondeu. Continuando a missão...")
                return result, provider_name
            last_error = "resposta inválida"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            _event("Fonte indisponível", f"{provider_name} respondeu {last_error}. Tentando outra fonte...")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = type(exc).__name__
            _event("Fonte indisponível", f"{provider_name} não respondeu. Tentando outra fonte...")
        except Exception as exc:
            last_error = type(exc).__name__
            _event("Fonte indisponível", f"{provider_name} falhou ({last_error}). Tentando outra fonte...")
    return None, last_error


def _fallback_nominatim(segment: str, city: str, limit: int) -> list[dict]:
    _event("Fallback ativado", "As fontes principais falharam. Tentando descoberta alternativa...")
    results: list[dict] = []
    seen: set[str] = set()
    queries = [f"{keyword}, {city}, Brasil" for keyword in _KEYWORDS.get(_normalize(segment), (_normalize(segment),))]
    for query in queries:
        params = urllib.parse.urlencode({
            "q": query,
            "format": "jsonv2",
            "limit": min(limit, 5),
            "countrycodes": "br",
            "extratags": 1,
            "namedetails": 1,
        })
        try:
            response = _request_json(f"https://nominatim.openstreetmap.org/search?{params}", timeout=15)
        except Exception:
            continue
        if not isinstance(response, list):
            continue
        for item in response:
            name = str((item.get("namedetails") or {}).get("name") or str(item.get("display_name", "")).split(",")[0]).strip()
            key = _normalize(name)
            if not name or key in seen:
                continue
            seen.add(key)
            tags = dict(item.get("extratags") or {})
            tags["name"] = name
            candidate = {"tags": tags}
            if _relevant(candidate, segment):
                results.append(candidate)
                if len(results) >= limit:
                    return results
    return results


def _search(segment: str, city: str, limit: int) -> list[dict]:
    bbox = _geocode_city(city)
    if bbox is None:
        raise ValueError("cidade")
    _event("Cidade localizada", f"Área de {city} identificada. Fazendo busca ampliada...")
    query = _build_query(segment, bbox)
    result, _ = _query_overpass(query)
    elements = result.get("elements", []) if isinstance(result, dict) else []

    accepted: list[dict] = []
    seen: set[str] = set()
    for item in elements:
        tags = item.get("tags") or {}
        name = str(tags.get("name", "")).strip()
        key = _normalize(name)
        if not name or key in seen:
            continue
        seen.add(key)
        if _relevant(item, segment):
            accepted.append(item)
            if len(accepted) >= limit:
                break
        else:
            with _lock:
                _state["rejected"] += 1
            _event("Resultado descartado", f"{name} não corresponde com segurança ao segmento '{segment}'.")

    if accepted:
        return accepted
    return _fallback_nominatim(segment, city, limit)


def _contact(tags: dict) -> str:
    for key in ("contact:whatsapp", "whatsapp", "contact:phone", "phone", "contact:email", "email", "contact:website", "website", "url"):
        value = tags.get(key)
        if value:
            return str(value).strip()[:120]
    return ""


def _enrich_contact(name: str, city: str, current: str) -> str:
    if current:
        return current
    params = urllib.parse.urlencode({
        "q": f"{name}, {city}, Brasil",
        "format": "jsonv2",
        "limit": 3,
        "countrycodes": "br",
        "extratags": 1,
    })
    try:
        results = _request_json(f"https://nominatim.openstreetmap.org/search?{params}", timeout=15)
        if isinstance(results, list):
            for result in results:
                found = _contact(result.get("extratags") or {})
                if found:
                    return found
    except Exception:
        pass
    return ""


def _exists(name: str, city: str) -> bool:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM leads WHERE lower(company_name)=lower(?) AND lower(city)=lower(?) LIMIT 1",
            (name.strip(), city.strip()),
        ).fetchone()
    return row is not None


def run_mission(segment: str, city: str, limit: int = 5) -> None:
    segment = segment.strip()
    city = city.strip()
    limit = max(1, min(int(limit), 10))
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
            _event("Pesquisa concluída", "Nenhuma fonte retornou lead confiável para essa busca.")
            return

        for place in places:
            tags = place.get("tags") or {}
            name = str(tags.get("name", "")).strip()
            if not name or _exists(name, city):
                continue
            _event("Lead confirmado", f"{name} passou pela validação. Procurando contato público...")
            contact = _enrich_contact(name, city, _contact(tags))
            _event("Enriquecimento", f"{name}: " + ("contato público encontrado." if contact else "contato não disponível nas fontes atuais."))
            lead = LeadCreate(
                company_name=name,
                segment=segment,
                city=city,
                contact=contact,
                source="EVO multi-source public data",
            )
            saved = create_lead(lead)
            with _lock:
                _state["saved"] += 1
            _event("Oportunidade adicionada", f"{name}: score {saved['score']}/100, status {saved['status']}.")

        with _lock:
            saved_count = _state["saved"]
            rejected_count = _state["rejected"]
        _event("Missão concluída", f"{saved_count} lead(s) novo(s); {rejected_count} resultado(s) descartado(s).")
    except ValueError:
        _event("Cidade não localizada", f"Não consegui localizar '{city}'. Use Cidade, UF.")
    except Exception as exc:
        _event("Missão interrompida", f"O EVO-01 encontrou uma falha inesperada ({type(exc).__name__}), mas o servidor continua online.")
    finally:
        with _lock:
            _state["running"] = False
            _state["last_run"] = datetime.now(timezone.utc).isoformat()
