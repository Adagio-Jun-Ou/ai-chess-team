import os

from dotenv import load_dotenv

load_dotenv()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")

PARTNER_MODEL = os.getenv("PARTNER_MODEL", "glm-5.3-flash")
OPPONENT_A_MODEL = os.getenv("OPPONENT_A_MODEL", "deepseek-v4-flash:0731")
OPPONENT_B_MODEL = os.getenv("OPPONENT_B_MODEL", "glm-5.3-flash")
AUTOPILOT_MODEL = os.getenv("AUTOPILOT_MODEL", "glm-5.3-flash")

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "45"))

# Spectate mode: 2v2 - Ava & Kai (White) vs Viktor & Mira (Black), no human needed.
SPECTATE = os.getenv("SPECTATE", "1") == "1"
