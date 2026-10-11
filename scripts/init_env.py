"""Create local secrets without displaying them or overwriting an existing file."""
import os
import secrets
from pathlib import Path

path = Path(__file__).resolve().parents[1] / ".env"
if path.exists():
    raise SystemExit(".env already exists; left unchanged")
contents = ("BOT_MODE=paper\nBOT_AUTOSTART=false\nBOT_TOKEN=" + secrets.token_urlsafe(32)
            + "\nCFAPIKEY=" + os.environ.get("CFAPIKEY", "") + "\n")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as file:
    file.write(contents)
print("Created .env. Open it locally to obtain BOT_TOKEN. No secrets were printed.")
