from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TranscriptionCreate(BaseModel):
    title: str | None = Field(None, max_length=200)
    text: str = Field(..., min_length=1)
    language: str | None = Field(None, max_length=32)


class TranscriptionUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    text: str | None = Field(None, min_length=1)
    language: str | None = Field(None, max_length=32)


class TranscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    text: str
    language: str | None
    source: str
    duration_seconds: float | None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _assume_utc(cls, v: datetime) -> datetime:
        # SQLite drops tzinfo; everything we store is UTC, so put it back
        # so the browser converts to local time correctly.
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v


class TranscriptionPage(BaseModel):
    items: list[TranscriptionOut]
    total: int
    skip: int
    limit: int
