from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str = Field(default="", max_length=50)
    description: str = Field(default="", max_length=500)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    category: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=500)
    is_archived: bool | None = None


class KnowledgeBaseOut(BaseModel):
    id: str
    name: str
    category: str
    description: str
    is_archived: bool
    note_count: int = 0
    created_at: datetime
    updated_at: datetime
