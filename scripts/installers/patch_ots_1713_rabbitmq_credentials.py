#!/usr/bin/env python3
"""Patch OpenTAKServer 1.7.13 Socket.IO RabbitMQ URLs to use configured credentials.

OTS 1.7.13 passes an unauthenticated amqp://host URL to Flask-SocketIO/Kombu
in app.py and cot_parser.py. Pika connections already use
OTS_RABBITMQ_USERNAME/OTS_RABBITMQ_PASSWORD, but Socket.IO does not.

This patch makes both Socket.IO message queue URLs use the configured internal
RabbitMQ service account so the default guest/guest account can be removed.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

MARKER = "# AIRSOFT_RABBITMQ_CREDENTIALS_V1"


def package_root() -> Path:
    spec = importlib.util.find_spec("opentakserver")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "Could not locate opentakserver. Run this with the OTS 1.7.13 venv Python."
        )
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(path.name + f".bak-rabbitmq-auth-{stamp}")
    shutil.copy2(path, dest)
    return dest


def ensure_quote_import(source: str) -> str:
    if "from urllib.parse import quote" in source:
        return source
    anchor = "import traceback\n"
    if anchor not in source:
        raise RuntimeError("Could not find import anchor for urllib.parse.quote")
    return source.replace(anchor, anchor + "from urllib.parse import quote\n", 1)


def rabbit_url_expr(indent: str) -> str:
    return (
        f'{indent}"amqp://{{}}:{{}}@{{}}//".format(\n'
        f'{indent}    quote(str(app.config.get("OTS_RABBITMQ_USERNAME") or ""), safe=""),\n'
        f'{indent}    quote(str(app.config.get("OTS_RABBITMQ_PASSWORD") or ""), safe=""),\n'
        f'{indent}    app.config.get("OTS_RABBITMQ_SERVER_ADDRESS"),\n'
        f'{indent})'
    )


def patch_app(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        return False

    old = '        message_queue="amqp://" + app.config.get("OTS_RABBITMQ_SERVER_ADDRESS"),'
    if old not in source:
        raise RuntimeError(f"Unexpected OTS app.py source; anchor missing: {path}")

    source = ensure_quote_import(source)
    replacement = (
        f"        {MARKER}\n"
        "        message_queue="
        + rabbit_url_expr("")[0:]
        + ","
    )

    # rabbit_url_expr starts directly with a quoted expression; indent continuation
    # lines to match the function call.
    replacement = replacement.replace("\n", "\n        ")
    replacement = replacement.replace(f"        {MARKER}\n        ", f"        {MARKER}\n        ", 1)
    source = source.replace(old, replacement, 1)
    path.write_text(source, encoding="utf-8")
    return True


def patch_cot_parser(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        return False

    old = '    sio = SocketIO(message_queue="amqp://" + app.config.get("OTS_RABBITMQ_SERVER_ADDRESS"))'
    if old not in source:
        raise RuntimeError(f"Unexpected OTS cot_parser.py source; anchor missing: {path}")

    source = ensure_quote_import(source)
    replacement = (
        f"    {MARKER}\n"
        "    sio = SocketIO(\n"
        "        message_queue=\n"
        + rabbit_url_expr("            ")
        + "\n"
        "    )"
    )
    source = source.replace(old, replacement, 1)
    path.write_text(source, encoding="utf-8")
    return True


def main() -> int:
    root = package_root()
    app_py = root / "app.py"
    cot_parser_py = root / "cot_parser" / "cot_parser.py"

    for path in (app_py, cot_parser_py):
        if not path.is_file():
            raise RuntimeError(f"Missing OTS source file: {path}")

    backups = []
    changed = []

    try:
        for path, patcher in ((app_py, patch_app), (cot_parser_py, patch_cot_parser)):
            text = path.read_text(encoding="utf-8")
            if MARKER not in text:
                backups.append((path, backup(path)))
            if patcher(path):
                changed.append(path)

        py_compile.compile(str(app_py), doraise=True)
        py_compile.compile(str(cot_parser_py), doraise=True)
    except Exception:
        for path, saved in backups:
            shutil.copy2(saved, path)
        raise

    if changed:
        print("Installed RabbitMQ credential patch; Python compile check passed.")
        for path in changed:
            print(f"Patched: {path}")
    else:
        print("RabbitMQ credential patch already installed; Python compile check passed.")

    for _, saved in backups:
        print(f"Backup: {saved}")

    print("Restart OpenTAKServer after configuring OTS_RABBITMQ_USERNAME/PASSWORD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
