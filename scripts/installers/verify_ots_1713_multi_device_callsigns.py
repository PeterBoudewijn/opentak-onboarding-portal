#!/usr/bin/env python3
"""Verify OTS multi-device callsign support without modifying production data."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from opentakserver.app import create_app
from opentakserver.extensions import db
from opentakserver.models.EUD import EUD
import opentakserver.eud_handler.EudHandler as eud_handler_module


MODEL_MARKER = "AIRSOFT_MULTI_DEVICE_CALLSIGN_MODEL_V1"
HANDLER_MARKER = "AIRSOFT_MULTI_DEVICE_CALLSIGN_ROUTING_V1"


def main() -> int:
    app = create_app(cli=True)
    ok = True

    with app.app_context():
        print("=== DATABASE ===")
        inspector = inspect(db.engine)

        constraints = [
            item
            for item in inspector.get_unique_constraints("euds")
            if set(item.get("column_names") or []) == {"callsign"}
        ]
        if constraints:
            print(
                "FAIL euds.callsign still has UNIQUE constraint(s): "
                + ", ".join(str(item.get("name")) for item in constraints)
            )
            ok = False
        else:
            print("PASS euds.callsign is not UNIQUE")

        indexes = inspector.get_indexes("euds")
        callsign_indexes = [
            item
            for item in indexes
            if set(item.get("column_names") or []) == {"callsign"}
            and not item.get("unique")
        ]
        if callsign_indexes:
            print("PASS non-unique euds.callsign index present")
        else:
            print("FAIL non-unique euds.callsign index missing")
            ok = False

        if EUD.__table__.c.callsign.unique:
            print("FAIL ORM model still marks EUD.callsign unique")
            ok = False
        else:
            print("PASS ORM model allows duplicate callsigns")

    print()
    print("=== RABBITMQ ROUTING SOURCE ===")
    handler_path = Path(eud_handler_module.__file__).resolve()
    source = handler_path.read_text(encoding="utf-8")

    checks = (
        (
            HANDLER_MARKER in source,
            "multi-device routing marker present",
        ),
        (
            "queue_declare(queue=self.callsign)" not in source,
            "no shared callsign queue declaration",
        ),
        (
            "basic_consume(\n                            queue=self.callsign"
            not in source,
            "no shared callsign queue consumer",
        ),
        (
            'exchange="dms", queue=self.uid, routing_key=self.callsign'
            in source,
            "callsign DMs route to per-device UID queue",
        ),
    )

    for passed, label in checks:
        print(f"{'PASS' if passed else 'FAIL'} {label}")
        ok = ok and passed

    print()
    if ok:
        print("RESULT: PASS - multiple devices may safely share one TAK callsign.")
        return 0

    print("RESULT: FAIL - multi-device callsign support needs correction.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
