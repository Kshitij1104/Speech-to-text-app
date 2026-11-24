"""Central configuration, read from environment variables."""
import os

# --- Database -------------------------------------------------------------
# Any SQLAlchemy URL works. Examples:
#   sqlite:///./transcriptions.db
#   postgresql://user:pass@host:5432/dbname
#   mysql+pymysql://user:pass@host:3306/dbname   (needs: pip install pymysql)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./transcriptions.db")

# Some hosts (Heroku, older Render) hand out "postgres://", which SQLAlchemy rejects.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# --- Speech-to-text -------------------------------------------------------
# "whisper" = faster-whisper (open source, self-hosted). "mock" = fake, for tests.
STT_BACKEND = os.getenv("STT_BACKEND", "whisper")
# tiny / base / small / medium / large-v3. Bigger = better Hindi+English, slower.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")  # "cpu", "cuda" or "auto"
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

# --- Real-time tuning -----------------------------------------------------
SPEECH_RMS_THRESHOLD = float(os.getenv("SPEECH_RMS_THRESHOLD", "0.012"))
SILENCE_SECONDS = float(os.getenv("SILENCE_SECONDS", "0.9"))   # pause that ends a sentence
PARTIAL_SECONDS = float(os.getenv("PARTIAL_SECONDS", "1.5"))   # how often to refresh live text
MAX_SEGMENT_SECONDS = float(os.getenv("MAX_SEGMENT_SECONDS", "25"))

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
