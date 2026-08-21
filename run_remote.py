"""Launch the isolated Cloudflare Tunnel origin through the project venv."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if (
    VENV_PYTHON.exists()
    and Path(sys.executable).resolve() != VENV_PYTHON.resolve()
    and not os.environ.get("SAIGE_REVIEWER_NO_VENV")
):
    os.execv(
        str(VENV_PYTHON),
        [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
    )

sys.path.insert(0, str(ROOT / "src"))

from saige_reviewer.remote_server import main


if __name__ == "__main__":
    main()
