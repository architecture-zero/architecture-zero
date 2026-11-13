"""Two startup lines about a mode setting name it and never its value.

PII_OUTPUT_MODE (off, warn, redact) and COMP_FIGURE_GUARD (off, redact) are
modes, not on/off settings, and a value that is not one of their words is not
refused: the output filter falls back to off and the comp guard to on, and the
start logs `pii_output_mode_invalid` or `comp_figure_guard_invalid` to say so
(app/runtime_config.py). Those two lines carried the value the host wrote. A
value on the wrong line of an env file can be a secret, and a log line is
read, shipped and kept, so each now names the variable and the words it
takes, and the value is nowhere in what the start printed or logged.

Each test says whether it is a FIX (it fails on the code from before) or a
CONTROL (it holds on both).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
MARKER = "Zq7-pasted-by-mistake"
LINES = {
    "PII_OUTPUT_MODE": ("pii_output_mode_invalid", "off|warn|redact", "redact"),
    "COMP_FIGURE_GUARD": ("comp_figure_guard_invalid", "off|redact", "redact"),
}

# The module whose import writes the lines, in a fresh interpreter: the lines
# are written once, when it loads. The child gets its own database file and
# log directory, and a stand-in for the vector store as conftest gives the
# suite.
_IMPORT = "\n".join((
    "import sys",
    "from unittest.mock import MagicMock",
    "sys.modules['chromadb'] = MagicMock(name='chromadb')",
    "import app.runtime_config",
    "print('IMPORTED')",
))


def _import_with(tmp_path, name: str, value: str):
    env = {k: v for k, v in os.environ.items() if k not in LINES}
    for var in ("DATA_DIR", "BACKUP_STATUS_DIR", "BOOT_HISTORY_DIR", "HELP_SYNC_STATE_DIR",
                "EVIDENCE_STATUS_DIR", "CHROMA_PATH", "FASTEMBED_CACHE", "KB_DIR"):
        env[var] = str(tmp_path / var.lower())
    env["LOG_DIR"] = str(tmp_path / "logs")
    env["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'the.db').as_posix()}"
    env["HISTORY_DB_PATH"] = str(tmp_path / "history.db")
    env["JWT_SECRET_KEY"] = "a-secret-for-the-mode-line-probe-only"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env[name] = value
    done = subprocess.run([sys.executable, "-c", _IMPORT], capture_output=True, text=True,
                          env=env, cwd=str(BACKEND), timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    assert done.stdout.strip().splitlines()[-1] == "IMPORTED"
    logged = tmp_path / "logs" / "app.log"
    kept = logged.read_text(encoding="utf-8") if logged.exists() else ""
    return done.stdout + done.stderr, kept


def _event(printed: str, event: str):
    """The fields of the JSON line named `event`, or None."""
    for line in printed.splitlines():
        line = line.strip()
        if line.startswith("{") and f'"event": "{event}"' in line:
            return json.loads(line)
    return None


@pytest.mark.parametrize("name", sorted(LINES))
def test_the_line_names_the_setting_and_never_the_value(tmp_path, name):
    """FIX. The value the host wrote is in neither the printed output nor
    the log file; the line names the variable and the words it takes, and
    says which one is in use."""
    event, takes, _ = LINES[name]
    printed, kept = _import_with(tmp_path, name, MARKER)
    fields = _event(printed, event)
    assert fields is not None, printed[-2000:]
    assert fields.get("variable") == name
    assert fields.get("takes") == takes
    assert fields.get("using") in takes.split("|")
    assert "value" not in fields
    assert MARKER.lower() not in printed.lower()
    assert MARKER.lower() not in kept.lower()


@pytest.mark.parametrize("name", sorted(LINES))
def test_a_word_the_mode_takes_writes_no_line(tmp_path, name):
    """CONTROL. One of the mode's own words: no line, as before."""
    event, _, word = LINES[name]
    printed, _ = _import_with(tmp_path, name, word)
    assert _event(printed, event) is None
