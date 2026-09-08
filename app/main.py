from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.schemas import LeadCreate
from app.services import create_lead, get_metrics, list_leads

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="EVO Sales", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def startup_event() -> None:
    init_db()


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "employee": "EVO-01", "activity": "aguardando tarefa"}


@app.get("/api/leads")
def api_list_leads() -> list[dict]:
    return list_leads()


@app.post("/api/leads", status_code=201)
def api_create_lead(lead: LeadCreate) -> dict:
    return create_lead(lead)


@app.get("/api/metrics")
def api_metrics() -> dict:
    return get_metrics()
