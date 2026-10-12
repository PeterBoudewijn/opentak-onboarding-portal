#!/usr/bin/env python3
"""Create or rotate permanent CO game-portal QR login tokens in .env.

The token values are written directly to .env and are deliberately not printed.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import secrets
import shutil
from datetime import datetime


KEYS = ("GAME_CO_QR_TOKEN_RED", "GAME_CO_QR_TOKEN_BLUE")


def update_env(path: Path, rotate: bool) -> tuple[list[str], list[str]]:
    if not path.exists():
        raise SystemExit(f".env not found: {path}")

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    values = {}
    positions = {}

    for index, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in KEYS:
            values[key] = value.strip()
            positions[key] = index

    created = []
    preserved = []
    for key in KEYS:
        if values.get(key) and not rotate:
            preserved.append(key)
            continue
        token = secrets.token_urlsafe(32)
        line = f"{key}={token}"
        if key in positions:
            lines[positions[key]] = line
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(line)
        created.append(key)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(path.name + f".bak.{timestamp}")
    shutil.copy2(path, backup)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return created, preserved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env", help="Path to portal .env file")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Replace existing CO QR tokens, invalidating previously printed QR codes",
    )
    args = parser.parse_args()

    path = Path(args.env).expanduser().resolve()
    created, preserved = update_env(path, args.rotate)

    for key in created:
        print(f"Generated {key}")
    for key in preserved:
        print(f"Kept existing {key}")
    print("Token values were written to .env and were not printed.")
    print("Restart the portal service after changing tokens.")


if __name__ == "__main__":
    main()
