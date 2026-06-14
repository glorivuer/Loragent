import os
from dotenv import load_dotenv

# Load local environment variables from .env if present
load_dotenv()

# Database Paths
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db")
DB_PATH = os.path.join(DB_DIR, "state.db")

# Ensure DB directory exists
os.makedirs(DB_DIR, exist_ok=True)

# Redis Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))

# Telegram Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # Default fallback chat ID

# Gemini Interactions API Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "gemini-3.5-flash")
ANTIGRAVITY_MODEL = os.getenv("ANTIGRAVITY_MODEL", "antigravity-preview-05-2026")

# Host Chrome CDP Remote Debugging URL
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222")

# Lock Mutex Timeout (in seconds)
LOCK_TIMEOUT = 900  # 15 minutes

# Chrome Restart/Drain Period Window
# Restart is scheduled at 03:59:00, draining begins at 03:50:00
DRAIN_START_HOUR = 3
DRAIN_START_MINUTE = 50
RESTART_HOUR = 3
RESTART_MINUTE = 59
