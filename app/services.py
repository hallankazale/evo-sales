from app.database import get_connection
from app.schemas import LeadCreate


def score_lead(lead: LeadCreate) -> int:
    score = 20
    if lead.contact:
        score += 25
    if lead.city:
        score += 10
    if lead.segment:
        score += 20
    if lead.source and lead.source != "manual":
        score += 10
    return min(score, 100)


def build_message(lead: LeadCreate) -> str:
    segment = f" do segmento de {lead.segment}" if lead.segment else ""
    city = f" em {lead.city}" if lead.city else ""
    return (
        f"Olá! Encontrei a {lead.company_name}{segment}{city} e percebi que talvez possamos "
        "ajudar a automatizar atendimento, captação e tarefas repetitivas usando IA. "
        "Posso te mostrar uma ideia simples aplicada ao seu negócio?"
    )


def create_lead(lead: LeadCreate) -> dict:
    score = score_lead(lead)
    message = build_message(lead)
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO leads (company_name, segment, city, contact, source, score, status, message)
            VALUES (?, ?, ?, ?, ?, ?, 'analisado', ?)
            """,
            (
                lead.company_name.strip(),
                lead.segment.strip(),
                lead.city.strip(),
                lead.contact.strip(),
                lead.source.strip(),
                score,
                message,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM leads WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def list_leads() -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM leads ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


def get_metrics() -> dict:
    with get_connection() as connection:
        total = connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        qualified = connection.execute("SELECT COUNT(*) FROM leads WHERE score >= 60").fetchone()[0]
        ready = connection.execute("SELECT COUNT(*) FROM leads WHERE message <> ''").fetchone()[0]
        clients = connection.execute("SELECT COUNT(*) FROM leads WHERE status = 'cliente'").fetchone()[0]
    return {
        "leads": total,
        "qualified": qualified,
        "messages": ready,
        "clients": clients,
    }
