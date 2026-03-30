from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent.parent / ".env")

HF_TOKEN = os.getenv("HF_TOKEN")
LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://192.168.1.76:1234/v1")
LM_STUDIO_API_KEY = os.getenv("LM_STUDIO_API_KEY", "lm-studio")
OBSIDIAN_VAULT = Path(os.getenv("OBSIDIAN_VAULT", "/home/aria/obsidian-vault"))
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))

# AuraScribe directories inside vault
VAULT_MEETINGS = OBSIDIAN_VAULT / "AuraScribe" / "Meetings"
VAULT_PEOPLE = OBSIDIAN_VAULT / "AuraScribe" / "People"
VAULT_DAILY = OBSIDIAN_VAULT / "AuraScribe" / "Daily"

# Local app data
APP_DIR = Path(__file__).parent.parent
DB_PATH = APP_DIR / "aurascribe.db"
RECORDINGS_DIR = APP_DIR / "recordings"

# Audio settings
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_DURATION = 30       # seconds per transcription chunk
VAD_THRESHOLD = 0.5
SILENCE_DURATION = 1.0    # seconds of silence before ending an utterance

# Transcription / diarization device
WHISPER_DEVICE = "cuda"
WHISPER_LANGUAGE = "en"

# Speaker diarization
DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"
MY_SPEAKER_LABEL = "Me"   # Label assigned after speaker enrollment
