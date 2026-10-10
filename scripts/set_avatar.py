#!/usr/bin/env python3
"""Installs the bot avatar via the Bot API (setMyProfilePhoto method).

Usage: .venv/bin/python scripts/set_avatar.py [path-to-png]
By default uses assets/lechonk_avatar.png.

Note: python-telegram-bot 22.8 can't upload a file for this method out of the
box (BadRequest), so this does a direct multipart request: the photo field is
{"type": "static", "photo": "attach://photo"} plus a "photo" file part.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import load_config


def main() -> None:
    cfg = load_config()
    if not cfg.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parent.parent / "assets" / "lechonk_avatar.png"
    )
    if not path.exists():
        raise SystemExit(f"no such file: {path}")

    url = f"https://api.telegram.org/bot{cfg.telegram_token}/setMyProfilePhoto"
    data = {"photo": json.dumps({"type": "static", "photo": "attach://photo"})}
    files = {"photo": (path.name, path.read_bytes(), "image/png")}
    try:
        r = httpx.post(url, data=data, files=files, timeout=60)
        ok = r.status_code == 200 and r.json().get("ok")
    except Exception as e:
        print("❌ Failed:", e)
        print("Set it manually via @BotFather: /setuserpic → file", path)
        return
    if ok:
        print("✅ Avatar installed")
    else:
        print("❌", r.status_code, r.text[:300])
        print("Set it manually via @BotFather: /setuserpic → file", path)


if __name__ == "__main__":
    main()
