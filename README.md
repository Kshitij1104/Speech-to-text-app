# Speech to Text: real-time Hindi + English transcription

A web app where you speak (Hindi, English, or a mix) and see the words appear live.
Every recording is saved to a SQL database and shown in a dashboard with full CRUD.

**Stack:** Python · FastAPI · WebSocket · [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (open-source Whisper, self-hosted) · SQLAlchemy (SQLite / PostgreSQL / MySQL) · vanilla JS frontend.

## Features
- **Live transcription**: browser mic → WebSocket → Whisper. Grey text is the in-progress sentence; it turns black when the sentence is finalised (after a ~1 s pause).
- **Multilingual**: modes for *Hindi + English (mixed)*, auto-detect, English only, Hindi only.
- **Dashboard**: list, search, paginate, create, edit, delete, copy, download `.txt`.
- **File upload**: transcribe an existing audio/video file.
- **REST API** with auto docs at `/docs`.
- Saves what was said even if the tab is closed mid-recording.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://localhost:8000   (the microphone works on localhost without HTTPS)
```
The first start downloads the Whisper model (~150 MB for `base`). Uses SQLite by default.

With PostgreSQL via Docker:
```bash
docker compose up --build      # http://localhost:8000
```

## Configuration (environment variables)
| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./transcriptions.db` | `postgresql://…` or `mysql+pymysql://…` (`pip install pymysql`) |
| `WHISPER_MODEL` | `base` | `tiny` (fast, weakest) → `small` (recommended for Hindi) → `medium` / `large-v3` (best, needs GPU or patience) |
| `WHISPER_DEVICE` | `cpu` | `cuda` if you have a GPU |
| `WHISPER_COMPUTE_TYPE` | `int8` | `float16` on GPU |
| `SILENCE_SECONDS` | `0.9` | pause length that ends a sentence |
| `SPEECH_RMS_THRESHOLD` | `0.012` | raise in noisy rooms |

## API
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/transcriptions?q=&skip=&limit=` | list / search |
| POST | `/api/transcriptions` | create |
| GET | `/api/transcriptions/{id}` | read |
| PUT | `/api/transcriptions/{id}` | update |
| DELETE | `/api/transcriptions/{id}` | delete |
| GET | `/api/transcriptions/{id}/download` | plain-text download |
| POST | `/api/transcribe-file` | upload audio → transcript (multipart: `file`, `mode`) |
| WS | `/ws/transcribe` | live stream (protocol documented in `app/main.py`) |
| GET | `/health` | health check |

## Deploy (Docker-based hosts)
- **Render:** push to GitHub → *New → Blueprint* → select repo (`render.yaml` provisions web service + Postgres). Needs ≥1 GB RAM for the `base` model; the free 512 MB tier only fits `WHISPER_MODEL=tiny`.
- **Railway / Fly.io / any VPS:** deploy the `Dockerfile`, set `DATABASE_URL`. HTTPS is required for microphone access (these hosts provide it).
- **Hugging Face Spaces (Docker, free 16 GB RAM):** a good free option, since it has enough memory for `small`.

## Tests
```bash
pip install -r requirements-dev.txt
pytest
```
Tests use a mock transcriber (`STT_BACKEND=mock`), so they run without downloading a model.

## How real-time works
1. The browser captures the mic with an AudioWorklet, converts to 16 kHz mono PCM16 and streams it over a WebSocket.
2. The server does simple energy-based voice detection. While you speak it re-decodes the current sentence every ~1.5 s (partial result); on a pause it decodes once more and sends the final sentence.
3. On *Stop*, all sentences are joined and saved as one row.

## Known limitations
- Whisper is not a true streaming model; "live" text is re-decoded chunks, so partial text can change as you speak.
- Hinglish mode pins Whisper to Hindi with a mixed-language prompt: Hindi comes out in Devanagari, English words in Latin script. Accuracy on `tiny`/`base` is modest; use `small` or larger for good results.
- No authentication; all users share one history. Add auth before exposing publicly with private data.
- No DB migrations (tables are created at startup); use Alembic for schema changes.
