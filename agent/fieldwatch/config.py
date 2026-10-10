import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Secrets live in agent/.env (gitignored): AWS_BEARER_TOKEN_BEDROCK, AWS_REGION, FIELDWATCH_MODEL ...
load_dotenv(ROOT / ".env")
FIXTURES = ROOT / "fixtures"
SESSIONS = ROOT / "sessions"

REGION = os.getenv("AWS_REGION", "us-west-2")

# Which model backs every agent. Set FIELDWATCH_PROVIDER to one of:
#   bedrock  (default) Bedrock, paid from the $100 free-tier credit. Model via FIELDWATCH_MODEL.
#   ollama   a local model served by Ollama at FIELDWATCH_OLLAMA_HOST. CPU-only on this laptop; slow.
PROVIDER = os.getenv("FIELDWATCH_PROVIDER", "bedrock").lower()

# Bedrock model ids. Cheapest to best:
#   us.amazon.nova-lite-v1:0                       ~free, fine for wiring tests
#   us.anthropic.claude-haiku-4-5-20251001-v1:0    cheap, good tool use; default for dev
#   us.anthropic.claude-sonnet-4-5-20250929-v1:0   best; use for the demo
MODEL_ID = os.getenv("FIELDWATCH_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

# Farm definition the tools read. The web UI points this at a per-farm file it writes.
FARM_FILE = Path(os.getenv("FIELDWATCH_FARM_FILE", str(FIXTURES / "demo_farm.json")))
DATA = ROOT / "data"

OLLAMA_HOST = os.getenv("FIELDWATCH_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("FIELDWATCH_OLLAMA_MODEL", "qwen3:8b")
