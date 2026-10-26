from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    kb_id: str | None = Field(default=None, max_length=64)
    source_type: str | None = Field(default=None, pattern="^(note|document)$")
    top_k: int = Field(default=10, ge=1, le=50)
    mode: str = Field(default="semantic", pattern="^(semantic|metadata)$")
