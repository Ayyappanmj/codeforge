from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from codeforge_shared.languages import UnsupportedLanguage, get_language

# Character caps are a cheap first line of defence; exact byte limits are enforced in the route
# using server-side configuration.
MAX_CHARS = 1024 * 1024


class ExecutionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(max_length=32)
    source_code: str = Field(min_length=1, max_length=MAX_CHARS)
    stdin: str = Field(default="", max_length=MAX_CHARS)

    @field_validator("language")
    @classmethod
    def _known_language(cls, v: str) -> str:
        try:
            get_language(v)
        except UnsupportedLanguage as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("source_code")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source_code must not be blank")
        return v


class ExecutionCreated(BaseModel):
    execution_id: uuid.UUID
    status: str


class ExecutionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    execution_id: uuid.UUID = Field(validation_alias="id")
    language: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    execution_duration_ms: int | None = None
    exit_code: int | None = None
    error_type: str | None = None


class ExecutionDetail(ExecutionSummary):
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    source_code: str = ""
    stdin: str = ""
    cancel_requested: bool = False
    attempt_count: int = 0


class ExecutionPage(BaseModel):
    items: list[ExecutionSummary]
    total: int
    page: int
    page_size: int


class CancelResponse(BaseModel):
    execution_id: uuid.UUID
    status: str
    cancel_requested: bool
