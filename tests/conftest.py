import os
import sys
import tempfile
from pathlib import Path

# Keep test data (clinic db, call records) out of ./data.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="zoft-test-"))
# Tests must never place a real call.
for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "PUBLIC_HOST"):
    os.environ[key] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
