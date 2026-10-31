"""Download the offline Vosk English model used by the RESOLVE speech-to-text path.

    python scripts/fetch_stt_model.py            # -> data/models/vosk-model-small-en-us-0.15 (~40 MB)

The server auto-discovers a model under data/models/ (or set RESOLVE_VOSK_MODEL=<dir>). `pip install vosk`
is also required (requirements-optional.txt). The model is Apache-2.0 licensed by Alpha Cephei; it is not
committed to this repository. The archive's SHA-256 is printed so the download can be recorded.
"""
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

NAME = "vosk-model-small-en-us-0.15"
URL = f"https://alphacephei.com/vosk/models/{NAME}.zip"
DEST = Path(__file__).resolve().parents[1] / "data" / "models"


def main() -> int:
    target = DEST / NAME
    if (target / "am").is_dir():
        print(f"already present: {target}")
        return 0
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"downloading {URL}")
    with urllib.request.urlopen(URL, timeout=300) as r:
        data = r.read()
    print(f"sha256 {hashlib.sha256(data).hexdigest()}  {len(data)} bytes")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.namelist():
            if m.startswith("/") or ".." in Path(m).parts:
                print(f"refusing unsafe archive member {m}", file=sys.stderr)
                return 2
        z.extractall(DEST)
    print(f"installed: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
