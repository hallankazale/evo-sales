from __future__ import annotations

import json
import threading
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


def _search_nominatim(segment: str, city: str, limit: int) -> list[dict]:
    queries = [
        f"{segment}, {city}, Brasil",
        f"{segment} {city} Brasil",
    ]
    for query in queries:
        params = urllib.parse.urlencode({
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "extratags": 1,
            "namedetails": 1,
            "limit": max(1, min(limit, 10)),
            "countrycodes": "br",
        })
        request = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{params}",
            headers={"User-Agent": "EVO-Sales/0.3 local-prospecting-demo"},
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            results = json.loads(response.read().decode("utf-8"))
        if results:
            return results
    return []


def _extract_company(place: dict) -> str:
    namedetails = place.get("namedetails") or {}
    name = namedetails.get("name") or namedetails.get("name:pt")
    if name:
        return str(name).strip()

    display_name = str(place.get("display_name", "")).strip()
    return display_name.split(",")[0].strip() if display_name else ""


def _extract_contact(place: dict) -> str:
    tags = place.get("extratags") or {}
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


def _already_exists(company_name: str) -> bool:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM leads WHERE lower(company_name) = lower(?) LIMIT 1",
            (company_name.strip(),),
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
        places = _search_nominatim(segment, city, limit)
        with _lock:
            _state["found"] = len(places)

        if not places:
            _event(
                "Pesquisa concluída",
                "A fonte pública não retornou empresas para essa combinação. Tente o segmento no singular e use cidade + UF.",
            )
            return

        for place in places:
            company = _extract_company(place)
            if not company or _already_exists(company):
                continue

            contact = _extract_contact(place)
            detail = "contato público localizado" if contact else "sem contato público cadastrado"
            _event("Empresa encontrada", f"Analisando {company} — {detail}...")

            lead = LeadCreate(
                company_name=company,
                segment=segment,
                city=city,
                contact=contact,
                source="OpenStreetMap/Nominatim",
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
            _event("Pesquisa concluída", "Resultados encontrados, mas nenhum novo lead válido foi adicionado.")
        else:
            _event("Missão concluída", f"Pesquisa finalizada. {saved_count} nova(s) oportunidade(s) adicionada(s).")
    except Exception as exc:
        _event("Falha na pesquisa", f"Não foi possível concluir a missão: {type(exc).__name__}.")
    finally:
        with _lock:
            _state["running"] = False
            _state["last_run"] = datetime.now(timezone.utc).isoformat()
