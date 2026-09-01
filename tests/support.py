"""Fixtures shared by the protocol and endpoint tests."""

from __future__ import annotations

SYSTEM = {
    "role": "system",
    "content": (
        "You are OpenClaude, a local coding agent.\n"
        "CWD: D:\\CodeWorks\\claude-web-api\n"
        "Date: 2026-07-25"
    ),
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read one host file without changing it.",
            "parameters": {
                "type": "object",
                "title": "ReadInput",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or workspace-relative path.",
                        "examples": ["README.md"],
                    }
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Execute a command in the host workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]
