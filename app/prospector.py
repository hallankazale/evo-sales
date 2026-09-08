from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

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


class _OSMParser(HTMLParser):
    pass


def _search_nominatim(segment: str, city: str, limit: int) -> list[dict]:
    # Nominatim is used conservatively: one user-triggered request, small result set,
    # descriptive User-Agent and no scraping loop. It returns public OSM place data.
    query = f"{segment} em {city}, Brasil"
    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": max(1, min(limit, 10)),
        "countrycodes": "br",
    })
    request = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "EVO-Sales/0.2 local-prospecting-demo"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


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
            _event("Pesquisa concluída", "Nenhuma oportunidade pública encontrada nessa busca.")
            return

        for place in places:
            display_name = str(place.get("display_name", "")).strip()
            address = place.get("address") or {}
            company = (
                address.get("amenity")
                or address.get("shop")
                or address.get("office")
                or display_name.split(",")[0]
            )
            company = str(company).strip()
            if not company or _already_exists(company):
                continue

            _event("Empresa encontrada", f"Analisando {company}...")
            lead = LeadCreate(
                company_name=company,
                segment=segment,
                city=city,
                contact="",
                source="OpenStreetMap/Nominatim",
            )
            saved = create_lead(lead)
            with _lock:
                _state["saved"] += 1
            _event(
                "Oportunidade adicionada",
                f"{company}: score {saved['score']}/100. Abordagem preparada para aprovação.",
            )

        _event("Missão concluída", f"Pesquisa finalizada. {_state['saved']} nova(s) oportunidade(s) adicionada(s).")
    except Exception as exc:
        _event("Falha na pesquisa", f"Não foi possível concluir a missão: {type(exc).__name__}.")
    finally:
        with _lock:
            _state["running"] = False
            _state["last_run"] = datetime.now(timezone.utc).isoformat()
