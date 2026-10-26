from pydantic import BaseModel, Field


class QARequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    kb_ids: list[str] = Field(default_factory=list, max_length=10)
    session_id: str | None = Field(default=None, max_length=64)


class QARetrieveRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    kb_ids: list[str] = Field(default_factory=list, max_length=10)
    top_k: int = Field(default=20, ge=1, le=50)
