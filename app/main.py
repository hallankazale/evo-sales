from pathlib import Path
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.database import init_db
from app.prospector import get_state, run_mission
from app.schemas import LeadCreate
from app.services import create_lead, get_metrics, list_leads

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="EVO Sales", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class MissionRequest(BaseModel):
    segment: str = Field(min_length=2, max_length=80)
    city: str = Field(min_length=2, max_length=100)
    limit: int = Field(default=5, ge=1, le=10)


@app.on_event("startup")
def startup_event() -> None:
    init_db()


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    state = get_state()
    return {
        "status": "ok",
        "employee": "EVO-01",
        "activity": state["activity"],
        "running": state["running"],
    }


@app.get("/api/agent")
def agent_state() -> dict:
    return get_state()


@app.post("/api/agent/run", status_code=202)
def start_agent_mission(mission: MissionRequest) -> dict:
    if get_state()["running"]:
        raise HTTPException(status_code=409, detail="EVO-01 já está executando uma missão")
    worker = threading.Thread(
        target=run_mission,
        args=(mission.segment, mission.city, mission.limit),
        daemon=True,
    )
    worker.start()
    return {"status": "started", "message": "Missão enviada ao EVO-01"}


@app.get("/api/leads")
def api_list_leads() -> list[dict]:
    return list_leads()


@app.post("/api/leads", status_code=201)
def api_create_lead(lead: LeadCreate) -> dict:
    return create_lead(lead)


@app.get("/api/metrics")
def api_metrics() -> dict:
    return get_metrics()
