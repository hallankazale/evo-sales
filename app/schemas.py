from pydantic import BaseModel, Field


class LeadCreate(BaseModel):
    company_name: str = Field(min_length=2, max_length=120)
    segment: str = Field(default="", max_length=80)
    city: str = Field(default="", max_length=80)
    contact: str = Field(default="", max_length=120)
    source: str = Field(default="manual", max_length=120)


class LeadOut(LeadCreate):
    id: int
    score: int
    status: str
    message: str
    created_at: str
