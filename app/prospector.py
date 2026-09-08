from __future__ import annotations

import json
import re
import threading
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from app.database import get_connection
from app.schemas import LeadCreate
from app.services import create_lead

_lock = threading.Lock()
_state = {"running": False, "activity": "Aguardando missão", "last_run": None, "found": 0, "saved": 0, "rejected": 0, "events": []}

_SEGMENT_TAGS = {
    "academia": [("leisure", "fitness_centre"), ("sport", "fitness"), ("leisure", "sports_centre")],
    "academias": [("leisure", "fitness_centre"), ("sport", "fitness"), ("leisure", "sports_centre")],
    "barbearia": [("shop", "hairdresser")], "salao": [("shop", "beauty"), ("shop", "hairdresser")],
    "restaurante": [("amenity", "restaurant")], "lanchonete": [("amenity", "fast_food")],
    "pizzaria": [("amenity", "restaurant")], "clinica": [("amenity", "clinic"), ("healthcare", "clinic")],
    "dentista": [("amenity", "dentist")], "mercado": [("shop", "supermarket"), ("shop", "convenience")],
    "supermercado": [("shop", "supermarket")], "oficina": [("shop", "car_repair")],
    "imobiliaria": [("office", "estate_agent")], "farmacia": [("amenity", "pharmacy")],
    "pet shop": [("shop", "pet")], "hotel": [("tourism", "hotel")],
}

_KEYWORDS = {
    "academia": ("academia", "fitness", "gym", "musculacao", "crossfit", "treino"),
    "academias": ("academia", "fitness", "gym", "musculacao", "crossfit", "treino"),
}


def _event(title: str, detail: str) -> None:
    event = {"title": title, "detail": detail, "time": datetime.now().strftime("%H:%M:%S")}
    with _lock:
        _state["activity"] = detail
        _state["events"] = ([event] + _state["events"])[:40]


def get_state() -> dict:
    with _lock:
        return {key: (list(value) if key == "events" else value) for key, value in _state.items()}


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value).strip().lower())
    return "".join(c for c in value if not unicodedata.combining(c))


def _request_json(url: str, *, data: bytes | None = None, timeout: int = 25) -> object:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "EVO-Sales/0.6 prospecting-agent", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _geocode_city(city: str) -> tuple[float, float, float, float] | None:
    params = urllib.parse.urlencode({"q": f"{city}, Brasil", "format": "jsonv2", "limit": 1, "countrycodes": "br"})
    result = _request_json(f"https://nominatim.openstreetmap.org/search?{params}")
    if not isinstance(result, list) or not result or len(result[0].get("boundingbox", [])) != 4:
        return None
    south, north, west, east = map(float, result[0]["boundingbox"])
    return south, west, north, east


def _build_query(segment: str, bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    box = f"({south},{west},{north},{east})"
    selectors = []
    for key, value in _SEGMENT_TAGS.get(_normalize(segment), []):
        for kind in ("node", "way", "relation"):
            selectors.append(f'{kind}["{key}"="{value}"]{box};')
    # Segunda estratégia: nomes comerciais que contenham termos fortes do segmento.
    for keyword in _KEYWORDS.get(_normalize(segment), (_normalize(segment),)):
        safe = keyword.replace('"', '\\"')
        for kind in ("node", "way", "relation"):
            selectors.append(f'{kind}["name"~"{safe}",i]{box};')
    return "[out:json][timeout:25];(" + "".join(selectors) + ");out center tags;"


def _relevant(item: dict, segment: str) -> bool:
    tags = item.get("tags") or {}
    expected = _SEGMENT_TAGS.get(_normalize(segment), [])
    # Tags específicas são evidência forte. sports_centre sozinho continua ambíguo.
    for pair in expected:
        if tags.get(pair[0]) == pair[1] and pair != ("leisure", "sports_centre"):
            return True
    text = _normalize(" ".join(str(tags.get(k, "")) for k in ("name", "description", "brand", "operator", "sport")))
    return any(word in text for word in _KEYWORDS.get(_normalize(segment), (_normalize(segment),)))


def _search(segment: str, city: str, limit: int) -> list[dict]:
    bbox = _geocode_city(city)
    if bbox is None:
        raise ValueError("cidade")
    _event("Cidade localizada", f"Área de {city} identificada. Fazendo busca ampliada...")
    query = _build_query(segment, bbox)
    payload = urllib.parse.urlencode({"data": query}).encode()
    result = _request_json("https://overpass-api.de/api/interpreter", data=payload, timeout=35)
    elements = result.get("elements", []) if isinstance(result, dict) else []
    accepted, seen = [], set()
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
            with _lock: _state["rejected"] += 1
            _event("Resultado descartado", f"{name} não corresponde com segurança ao segmento '{segment}'.")
    return accepted


def _contact(tags: dict) -> str:
    for key in ("contact:whatsapp", "whatsapp", "contact:phone", "phone", "contact:email", "email", "contact:website", "website", "url"):
        if tags.get(key): return str(tags[key]).strip()[:120]
    return ""


def _enrich_contact(name: str, city: str, current: str) -> str:
    if current:
        return current
    # Nominatim pode ter metadados extras diferentes do objeto Overpass original.
    params = urllib.parse.urlencode({"q": f"{name}, {city}, Brasil", "format": "jsonv2", "limit": 3, "countrycodes": "br", "extratags": 1})
    try:
        results = _request_json(f"https://nominatim.openstreetmap.org/search?{params}", timeout=15)
        if isinstance(results, list):
            for result in results:
                tags = result.get("extratags") or {}
                found = _contact(tags)
                if found:
                    return found
    except Exception:
        pass
    return ""


def _exists(name: str, city: str) -> bool:
    with get_connection() as conn:
        return conn.execute("SELECT 1 FROM leads WHERE lower(company_name)=lower(?) AND lower(city)=lower(?) LIMIT 1", (name.strip(), city.strip())).fetchone() is not None


def run_mission(segment: str, city: str, limit: int = 5) -> None:
    segment, city, limit = segment.strip(), city.strip(), max(1, min(int(limit), 10))
    with _lock:
        if _state["running"]: return
        _state.update({"running": True, "found": 0, "saved": 0, "rejected": 0})
    try:
        _event("Missão iniciada", f"Pesquisando {segment} em {city}...")
        places = _search(segment, city, limit)
        with _lock: _state["found"] = len(places)
        if not places:
            _event("Pesquisa concluída", "A busca ampliada não encontrou lead confiável nessa fonte pública.")
            return
        for place in places:
            tags = place.get("tags") or {}
            name = str(tags.get("name", "")).strip()
            if not name or _exists(name, city): continue
            _event("Lead confirmado", f"{name} passou pela validação do segmento. Procurando contato público...")
            contact = _enrich_contact(name, city, _contact(tags))
            _event("Enriquecimento", f"{name}: " + ("contato público encontrado." if contact else "nenhum contato público disponível nessa fonte."))
            lead = LeadCreate(company_name=name, segment=segment, city=city, contact=contact, source="OpenStreetMap/Overpass+Nominatim")
            saved = create_lead(lead)
            with _lock: _state["saved"] += 1
            _event("Oportunidade adicionada", f"{name}: score {saved['score']}/100, status {saved['status']}.")
        with _lock: saved_count, rejected = _state["saved"], _state["rejected"]
        _event("Missão concluída", f"{saved_count} lead(s) novo(s); {rejected} resultado(s) descartado(s).")
    except ValueError:
        _event("Cidade não localizada", f"Não consegui localizar '{city}'. Use Cidade, UF.")
    except Exception as exc:
        _event("Falha na pesquisa", f"Missão interrompida: {type(exc).__name__}.")
    finally:
        with _lock:
            _state["running"] = False
            _state["last_run"] = datetime.now(timezone.utc).isoformat()
