from pathlib import Path

from dotenv import load_dotenv

# Load local settings before any module reads the environment (the database
# URL is read at import time). Variables already set in the process win, so
# exported values and test fixtures are never overridden by the file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
