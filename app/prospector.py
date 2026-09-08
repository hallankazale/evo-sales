from __future__ import annotations

import html
import json
import re
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
_state = {"running": False, "activity": "Aguardando missão", "last_run": None, "found": 0, "saved": 0, "rejected": 0, "provider": None, "events": []}

_OVERPASS_PROVIDERS = (
    ("Overpass DE", "https://overpass-api.de/api/interpreter"),
    ("Overpass Kumi", "https://overpass.kumi.systems/api/interpreter"),
)
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
_KEYWORDS = {"academia": ("academia", "fitness", "gym", "musculacao", "crossfit", "treino"), "academias": ("academia", "fitness", "gym", "musculacao", "crossfit", "treino")}


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
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "EVO-Sales/0.8 prospecting-agent", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_text(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 EVO-Sales/0.8", "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def _geocode_city(city: str) -> tuple[float, float, float, float] | None:
    params = urllib.parse.urlencode({"q": f"{city}, Brasil", "format": "jsonv2", "limit": 1, "countrycodes": "br"})
    result = _request_json(f"https://nominatim.openstreetmap.org/search?{params}", timeout=18)
    if not isinstance(result, list) or not result or len(result[0].get("boundingbox") or []) != 4:
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
    text = _normalize(" ".join(str(tags.get(k, "")) for k in ("name", "description", "brand", "operator", "sport", "snippet")))
    return any(word in text for word in _KEYWORDS.get(_normalize(segment), (_normalize(segment),)))


def _query_overpass(query: str) -> dict | None:
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    for provider_name, endpoint in _OVERPASS_PROVIDERS:
        _event("Consultando fonte", f"Tentando {provider_name}...")
        try:
            result = _request_json(endpoint, data=payload, timeout=35)
            if isinstance(result, dict):
                with _lock: _state["provider"] = provider_name
                _event("Fonte disponível", f"{provider_name} respondeu. Continuando a missão...")
                return result
        except Exception:
            _event("Fonte indisponível", f"{provider_name} não respondeu corretamente. Tentando outra fonte...")
    return None


def _fallback_nominatim(segment: str, city: str, limit: int) -> list[dict]:
    _event("Busca alternativa", "Consultando índice geográfico alternativo...")
    results, seen = [], set()
    for keyword in _KEYWORDS.get(_normalize(segment), (_normalize(segment),)):
        params = urllib.parse.urlencode({"q": f"{keyword}, {city}, Brasil", "format": "jsonv2", "limit": min(limit, 5), "countrycodes": "br", "extratags": 1, "namedetails": 1})
        try: response = _request_json(f"https://nominatim.openstreetmap.org/search?{params}", timeout=15)
        except Exception: continue
        if not isinstance(response, list): continue
        for item in response:
            name = str((item.get("namedetails") or {}).get("name") or str(item.get("display_name", "")).split(",")[0]).strip()
            key = _normalize(name)
            if not name or key in seen: continue
            seen.add(key)
            tags = dict(item.get("extratags") or {}); tags["name"] = name
            candidate = {"tags": tags}
            if _relevant(candidate, segment): results.append(candidate)
            if len(results) >= limit: return results
    return results


def _fallback_web(segment: str, city: str, limit: int) -> list[dict]:
    # Last-resort discovery from a public HTML search result. We only store business
    # names/snippets and public destination URLs; no login, personal data, or bypassing.
    _event("Descoberta web", "As bases geográficas foram insuficientes. Procurando empresas na web pública...")
    query = urllib.parse.quote_plus(f'"{segment}" "{city}" empresa')
    url = f"https://www.google.com/search?q={query}&num={min(limit * 3, 20)}&hl=pt-BR"
    try:
        page = _request_text(url, timeout=20)
    except Exception:
        _event("Web indisponível", "A busca web pública não respondeu nesta tentativa.")
        return []
    candidates, seen = [], set()
    # Google result headings are exposed as h3; nearby anchor gives destination URL.
    pattern = re.compile(r'<a[^>]+href="(?:/url\?q=)?([^"&]+)[^>]*>.*?<h3[^>]*>(.*?)</h3>', re.I | re.S)
    for raw_url, raw_title in pattern.findall(page):
        title = re.sub(r"<[^>]+>", " ", raw_title)
        title = html.unescape(re.sub(r"\s+", " ", title)).strip()
        destination = html.unescape(urllib.parse.unquote(raw_url)).strip()
        key = _normalize(title)
        if not title or key in seen: continue
        seen.add(key)
        tags = {"name": title, "website": destination, "snippet": f"{title} {segment} {city}"}
        candidate = {"tags": tags}
        if _relevant(candidate, segment):
            candidates.append(candidate)
            _event("Candidato web", f"{title} encontrado na web pública. Validando...")
        if len(candidates) >= limit: break
    return candidates


def _search(segment: str, city: str, limit: int) -> list[dict]:
    bbox = _geocode_city(city)
    if bbox is None: raise ValueError("cidade")
    _event("Cidade localizada", f"Área de {city} identificada. Iniciando descoberta multi-fonte...")
    result = _query_overpass(_build_query(segment, bbox))
    elements = result.get("elements", []) if isinstance(result, dict) else []
    accepted, seen = [], set()
    for item in elements:
        tags = item.get("tags") or {}; name = str(tags.get("name", "")).strip(); key = _normalize(name)
        if not name or key in seen: continue
        seen.add(key)
        if _relevant(item, segment): accepted.append(item)
        else:
            with _lock: _state["rejected"] += 1
            _event("Resultado descartado", f"{name} não corresponde ao segmento '{segment}'.")
        if len(accepted) >= limit: return accepted
    for candidate in _fallback_nominatim(segment, city, limit):
        name = _normalize((candidate.get("tags") or {}).get("name", ""))
        if name and name not in seen: accepted.append(candidate); seen.add(name)
        if len(accepted) >= limit: return accepted
    if len(accepted) < limit:
        for candidate in _fallback_web(segment, city, limit - len(accepted)):
            name = _normalize((candidate.get("tags") or {}).get("name", ""))
            if name and name not in seen: accepted.append(candidate); seen.add(name)
            if len(accepted) >= limit: break
    return accepted


def _contact(tags: dict) -> str:
    for key in ("contact:whatsapp", "whatsapp", "contact:phone", "phone", "contact:email", "email", "contact:website", "website", "url"):
        if tags.get(key): return str(tags[key]).strip()[:120]
    return ""


def _enrich_contact(name: str, city: str, current: str) -> str:
    if current: return current
    params = urllib.parse.urlencode({"q": f"{name}, {city}, Brasil", "format": "jsonv2", "limit": 3, "countrycodes": "br", "extratags": 1})
    try:
        results = _request_json(f"https://nominatim.openstreetmap.org/search?{params}", timeout=15)
        if isinstance(results, list):
            for result in results:
                found = _contact(result.get("extratags") or {})
                if found: return found
    except Exception: pass
    return ""


def _exists(name: str, city: str) -> bool:
    with get_connection() as conn:
        return conn.execute("SELECT 1 FROM leads WHERE lower(company_name)=lower(?) AND lower(city)=lower(?) LIMIT 1", (name.strip(), city.strip())).fetchone() is not None


def run_mission(segment: str, city: str, limit: int = 5) -> None:
    segment, city, limit = segment.strip(), city.strip(), max(1, min(int(limit), 10))
    with _lock:
        if _state["running"]: return
        _state.update({"running": True, "found": 0, "saved": 0, "rejected": 0, "provider": None})
    try:
        _event("Missão iniciada", f"Pesquisando {segment} em {city}...")
        places = _search(segment, city, limit)
        with _lock: _state["found"] = len(places)
        if not places:
            _event("Pesquisa concluída", "Nenhuma das fontes públicas retornou lead confiável nesta missão.")
            return
        for place in places:
            tags = place.get("tags") or {}; name = str(tags.get("name", "")).strip()
            if not name or _exists(name, city): continue
            _event("Lead confirmado", f"{name} passou pela validação. Procurando contato público...")
            contact = _enrich_contact(name, city, _contact(tags))
            _event("Enriquecimento", f"{name}: " + ("canal público encontrado." if contact else "contato não disponível nas fontes atuais."))
            saved = create_lead(LeadCreate(company_name=name, segment=segment, city=city, contact=contact, source="EVO multi-source public discovery"))
            with _lock: _state["saved"] += 1
            _event("Oportunidade adicionada", f"{name}: score {saved['score']}/100, status {saved['status']}.")
        with _lock: saved_count, rejected_count = _state["saved"], _state["rejected"]
        _event("Missão concluída", f"{saved_count} lead(s) novo(s); {rejected_count} resultado(s) descartado(s).")
    except ValueError:
        _event("Cidade não localizada", f"Não consegui localizar '{city}'. Use Cidade, UF.")
    except Exception as exc:
        _event("Missão interrompida", f"Falha inesperada ({type(exc).__name__}), mas o servidor continua online.")
    finally:
        with _lock:
            _state["running"] = False
            _state["last_run"] = datetime.now(timezone.utc).isoformat()
