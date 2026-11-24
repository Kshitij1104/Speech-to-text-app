import asyncio
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import config, models, schemas
from .db import Base, SessionLocal, engine, get_db
from .stt import MODES, SAMPLE_RATE, get_transcriber, load_audio

log = logging.getLogger("speech2text")
logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    # Load the model in the background so the server starts (and health checks pass) immediately.
    asyncio.get_running_loop().run_in_executor(None, get_transcriber().warmup)
    yield


app = FastAPI(title="Speech to Text", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "stt_backend": config.STT_BACKEND, "model": config.WHISPER_MODEL}


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------
def _default_title(text: str) -> str:
    snippet = " ".join(text.split())[:60]
    return snippet or "Untitled"


@app.post("/api/transcriptions", response_model=schemas.TranscriptionOut, status_code=201)
def create_transcription(payload: schemas.TranscriptionCreate, db: Session = Depends(get_db)):
    row = models.Transcription(
        title=(payload.title or "").strip() or _default_title(payload.text),
        text=payload.text.strip(),
        language=payload.language,
        source="manual",
    )
    db.add(row)
    db.commit()
    return row


@app.get("/api/transcriptions", response_model=schemas.TranscriptionPage)
def list_transcriptions(
    q: str | None = Query(None, description="Search in title and text"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(models.Transcription)
    count_stmt = select(func.count()).select_from(models.Transcription)
    if q:
        like = f"%{q}%"
        cond = or_(models.Transcription.title.ilike(like), models.Transcription.text.ilike(like))
        stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
    total = db.scalar(count_stmt) or 0
    items = db.scalars(
        stmt.order_by(models.Transcription.created_at.desc(), models.Transcription.id.desc())
        .offset(skip)
        .limit(limit)
    ).all()
    return {"items": items, "total": total, "skip": skip, "limit": limit}


def _get_or_404(db: Session, tid: int) -> models.Transcription:
    row = db.get(models.Transcription, tid)
    if row is None:
        raise HTTPException(404, "Transcription not found")
    return row


@app.get("/api/transcriptions/{tid}", response_model=schemas.TranscriptionOut)
def get_transcription(tid: int, db: Session = Depends(get_db)):
    return _get_or_404(db, tid)


@app.put("/api/transcriptions/{tid}", response_model=schemas.TranscriptionOut)
def update_transcription(tid: int, payload: schemas.TranscriptionUpdate, db: Session = Depends(get_db)):
    row = _get_or_404(db, tid)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(422, "Nothing to update")
    for key, value in data.items():
        if isinstance(value, str):
            value = value.strip()
        setattr(row, key, value)
    db.commit()
    return row


@app.delete("/api/transcriptions/{tid}", status_code=204)
def delete_transcription(tid: int, db: Session = Depends(get_db)):
    db.delete(_get_or_404(db, tid))
    db.commit()


@app.get("/api/transcriptions/{tid}/download", response_class=PlainTextResponse)
def download_transcription(tid: int, db: Session = Depends(get_db)):
    row = _get_or_404(db, tid)
    return PlainTextResponse(
        row.text,
        headers={"Content-Disposition": f'attachment; filename="transcription-{row.id}.txt"'},
    )


# --------------------------------------------------------------------------
# File upload transcription
# --------------------------------------------------------------------------
@app.post("/api/transcribe-file", response_model=schemas.TranscriptionOut, status_code=201)
async def transcribe_file(
    file: UploadFile = File(...),
    mode: str = Form("hinglish"),
    db: Session = Depends(get_db),
):
    if mode not in MODES:
        raise HTTPException(422, f"mode must be one of {list(MODES)}")
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    suffix = Path(file.filename or "").suffix[:10] or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                tmp.close()
                os.unlink(tmp.name)
                raise HTTPException(413, f"File larger than {config.MAX_UPLOAD_MB} MB")
            tmp.write(chunk)
    try:
        try:
            audio = await asyncio.to_thread(load_audio, tmp.name)
        except Exception as exc:  # undecodable file
            raise HTTPException(422, f"Could not read audio: {exc}")
        text, lang = await asyncio.to_thread(get_transcriber().transcribe, audio, mode)
    finally:
        os.unlink(tmp.name)
    if not text:
        raise HTTPException(422, "No speech detected in the file")
    row = models.Transcription(
        title=(file.filename or _default_title(text))[:200],
        text=text,
        language=lang,
        source="upload",
        duration_seconds=round(len(audio) / SAMPLE_RATE, 2),
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------
# Real-time transcription over WebSocket
#
# Protocol
#   client -> server  text   {"type":"start","mode":"hinglish","title":"optional"}
#   client -> server  binary 16 kHz mono PCM, int16 little-endian, any chunk size
#   client -> server  text   {"type":"stop"}
#   server -> client  {"type":"ready"}
#   server -> client  {"type":"partial","text":"..."}    live text of the current sentence
#   server -> client  {"type":"final","text":"..."}      a finished sentence
#   server -> client  {"type":"saved","transcription":{...}}   stored in the DB
#   server -> client  {"type":"error","message":"..."}
# --------------------------------------------------------------------------
class Streamer:
    """Turns a stream of PCM chunks into partial / final transcripts."""

    def __init__(self, mode: str):
        self.mode = mode
        self.stt = get_transcriber()
        self.buf: list[np.ndarray] = []
        self.buf_len = 0
        self.silence = 0.0          # trailing silence (seconds)
        self.heard_speech = False
        self.since_partial = 0.0
        self.sentences: list[str] = []
        self.total_samples = 0
        self.language: str | None = None

    def _audio(self) -> np.ndarray:
        return np.concatenate(self.buf) if self.buf else np.zeros(0, dtype=np.float32)

    async def _decode(self) -> str:
        audio = self._audio()
        if len(audio) < SAMPLE_RATE * 0.3:
            return ""
        text, lang = await asyncio.to_thread(self.stt.transcribe, audio, self.mode)
        if lang:
            self.language = lang
        return text

    def _reset(self):
        self.buf, self.buf_len = [], 0
        self.silence, self.since_partial, self.heard_speech = 0.0, 0.0, False

    async def feed(self, pcm: bytes) -> list[dict]:
        events: list[dict] = []
        if len(pcm) < 2:
            return events
        chunk = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        secs = len(chunk) / SAMPLE_RATE
        self.total_samples += len(chunk)
        is_speech = float(np.sqrt(np.mean(chunk**2))) > config.SPEECH_RMS_THRESHOLD

        if not self.heard_speech and not is_speech:
            return events  # ignore leading silence
        self.heard_speech = True
        self.buf.append(chunk)
        self.buf_len += len(chunk)
        self.silence = 0.0 if is_speech else self.silence + secs
        self.since_partial += secs

        buf_secs = self.buf_len / SAMPLE_RATE
        if self.silence >= config.SILENCE_SECONDS or buf_secs >= config.MAX_SEGMENT_SECONDS:
            events += await self.flush()
        elif self.since_partial >= config.PARTIAL_SECONDS:
            self.since_partial = 0.0
            text = await self._decode()
            if text:
                events.append({"type": "partial", "text": text})
        return events

    async def flush(self) -> list[dict]:
        events: list[dict] = []
        if self.buf:
            text = await self._decode()
            if text:
                self.sentences.append(text)
                events.append({"type": "final", "text": text})
        self._reset()
        return events

    @property
    def full_text(self) -> str:
        return " ".join(self.sentences).strip()

    @property
    def duration(self) -> float:
        return round(self.total_samples / SAMPLE_RATE, 2)


@app.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket):
    await ws.accept()
    streamer: Streamer | None = None
    title: str | None = None
    saved = False

    async def save_and_report():
        nonlocal saved
        if saved or streamer is None:
            return
        saved = True
        for ev in await streamer.flush():
            await ws.send_json(ev)
        text = streamer.full_text
        if not text:
            await ws.send_json({"type": "saved", "transcription": None})
            return
        with SessionLocal() as db:
            row = models.Transcription(
                title=(title or "").strip() or _default_title(text),
                text=text,
                language=streamer.language,
                source="live",
                duration_seconds=streamer.duration,
            )
            db.add(row)
            db.commit()
            out = schemas.TranscriptionOut.model_validate(row).model_dump(mode="json")
        await ws.send_json({"type": "saved", "transcription": out})

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                data = json.loads(msg["text"])
                kind = data.get("type")
                if kind == "start":
                    mode = data.get("mode", "hinglish")
                    if mode not in MODES:
                        await ws.send_json({"type": "error", "message": f"Unknown mode '{mode}'"})
                        continue
                    streamer, title, saved = Streamer(mode), data.get("title"), False
                    await ws.send_json({"type": "ready"})
                elif kind == "stop":
                    await save_and_report()
            elif msg.get("bytes") is not None:
                if streamer is None:
                    await ws.send_json({"type": "error", "message": "Send a 'start' message first"})
                    continue
                for ev in await streamer.feed(msg["bytes"]):
                    await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # keep the server alive on any client-side weirdness
        log.exception("websocket error")
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        # Browser closed mid-recording: still keep what was said.
        if streamer is not None and not saved:
            try:
                saved = True
                await streamer.flush()
                if streamer.full_text:
                    with SessionLocal() as db:
                        db.add(
                            models.Transcription(
                                title=(title or "").strip() or _default_title(streamer.full_text),
                                text=streamer.full_text,
                                language=streamer.language,
                                source="live",
                                duration_seconds=streamer.duration,
                            )
                        )
                        db.commit()
            except Exception:
                log.exception("failed to save on disconnect")


# --------------------------------------------------------------------------
# Frontend
# --------------------------------------------------------------------------
@app.get("/static/worklet.js", include_in_schema=False)
def serve_worklet():
    return FileResponse(
        STATIC_DIR / "worklet.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"}
    )

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
