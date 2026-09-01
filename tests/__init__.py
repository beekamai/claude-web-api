"""Test package setup.

The runtime opens its telemetry database when the package is imported, so the
redirect to a temporary file has to happen before any test module imports
claude_web_api. unittest imports this package first, which is the only place
that ordering is guaranteed.
"""

import os
import tempfile
from pathlib import Path

_TELEMETRY_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="claude-web-api-tests-"
)
os.environ["OPENCLAUDE_TELEMETRY_DB"] = str(
    Path(_TELEMETRY_DIRECTORY.name) / "telemetry.sqlite3"
)
