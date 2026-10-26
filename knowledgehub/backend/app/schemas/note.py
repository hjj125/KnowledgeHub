from pydantic import BaseModel, Field


class NoteCreate(BaseModel):
    kb_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    content_md: str = Field(default="", max_length=500_000)
    tags: list[str] = Field(default_factory=list)
    properties: dict[str, object] = Field(default_factory=dict)
    # 来源文档（草稿保存为笔记时传入）：图谱中建立 笔记-[DERIVED_FROM]->文档 边
    source_document_id: str | None = Field(default=None, max_length=64)


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content_md: str | None = Field(default=None, max_length=500_000)
    tags: list[str] | None = None
    properties: dict[str, object] | None = None
    expected_content_version: int = Field(gt=0, description="客户端持有的版本号；不匹配返回 409")


class NoteOut(BaseModel):
    id: str
    kb_id: str
    title: str
    content_md: str
    tags: list[str]
    properties: dict[str, object]
    linked_note_ids: list[str]
    content_version: int
    index_status: str
    created_at: str
    updated_at: str


class NoteVersionOut(BaseModel):
    version: int
    title: str
    content_md: str
    tags: list[str]
    changed_at: str
