"""Minimal client examples."""

import json
import urllib.request

BASE = "http://127.0.0.1:8765"


def chat(message: str, new_chat: bool = False) -> str:
    data = json.dumps({"message": message, "new_chat": new_chat}).encode()
    req = urllib.request.Request(
        f"{BASE}/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["response"]


def openai_style(message: str) -> str:
    data = json.dumps(
        {
            "model": "claude-web",
            "messages": [{"role": "user", "content": message}],
        }
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["choices"][0]["message"]["content"]


if __name__ == "__main__":
    print(chat("Ответь одним словом: ping?", new_chat=True))
