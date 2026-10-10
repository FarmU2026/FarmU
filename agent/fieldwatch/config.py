import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
SESSIONS = ROOT / "sessions"

REGION = os.getenv("AWS_REGION", "us-west-2")
MODEL_ID = os.getenv("FIELDWATCH_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
