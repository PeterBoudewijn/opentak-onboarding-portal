#!/usr/bin/env python3
"""Allow multiple TAK devices to share the same callsign safely on OTS 1.7.13.

Why this is needed:
- EUD.callsign is UNIQUE in upstream OTS 1.7.13.
- Airsoft account switching can legitimately result in two physical devices
  using the same account/callsign (for example 3D19, CORood or COBlauw).
- The first CoT from the second device then fails while OTS tries to update the
  EUD row to the already-used callsign.
- Upstream direct-message routing also consumes a shared RabbitMQ queue named
  after the callsign. Multiple consumers on that one queue would load-balance
  messages instead of delivering them to every device.

This patch:
1. removes the UNIQUE constraint on euds.callsign while keeping a normal index;
2. changes the ORM model so future schema creation does not recreate uniqueness;
3. routes both UID-addressed and callsign-addressed DMs into each device's UID
   queue, so every device sharing a callsign receives callsign-addressed DMs.

The patch is idempotent and PostgreSQL-specific for the schema change because
this production OTS deployment uses PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER_MODEL = "# AIRSOFT_MULTI_DEVICE_CALLSIGN_MODEL_V1"
MARKER_HANDLER = "# AIRSOFT_MULTI_DEVICE_CALLSIGN_ROUTING_V1"


def package_root() -> Path:
    spec = importlib.util.find_spec("opentakserver")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "Could not locate opentakserver. Run with the OTS 1.7.13 venv Python."
        )
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def backup(path: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = path.with_name(path.name + f".bak-{label}-{stamp}")
    shutil.copy2(path, saved)
    return saved


def patch_model(source: str) -> str:
    if MARKER_MODEL in source:
        return source

    old = (
        "    callsign: Mapped[str] = mapped_column("
        "String(255), nullable=True, unique=True)\n"
    )
    new = (
        f"    {MARKER_MODEL}\n"
        "    # Multiple physical TAK clients may intentionally share one account/callsign.\n"
        "    callsign: Mapped[str] = mapped_column(String(255), nullable=True, unique=False)\n"
    )

    if old not in source:
        raise RuntimeError("EUD.callsign unique model anchor missing")

    return source.replace(old, new, 1)


def patch_handler(source: str) -> str:
    if MARKER_HANDLER in source:
        return source

    # The production EudHandler already contains the airsoft team-binding
    # patch, so do not depend on a large pristine-upstream block. Change only
    # the three callsign-queue operations we actually need to replace.
    declare_line = "                    self.rabbit_channel.queue_declare(queue=self.callsign)\n"
    uid_declare_line = "                    self.rabbit_channel.queue_declare(queue=self.uid)\n"

    if declare_line in source:
        source = source.replace(
            declare_line,
            "                    "
            + MARKER_HANDLER
            + "\n"
            + "                    # A callsign may be shared; only declare the per-device UID queue.\n",
            1,
        )
    elif uid_declare_line in source:
        # Compatibility with an already-customized handler that has removed the
        # shared declaration by another route but does not yet carry our marker.
        source = source.replace(
            uid_declare_line,
            "                    "
            + MARKER_HANDLER
            + "\n"
            + uid_declare_line,
            1,
        )
    else:
        raise RuntimeError("RabbitMQ UID queue declaration anchor missing")

    old_callsign_bind = """                        self.rabbit_channel.queue_bind(
                            exchange="dms", queue=self.callsign, routing_key=self.callsign
                        )
"""
    new_callsign_bind = """                        self.rabbit_channel.queue_bind(
                            exchange="dms", queue=self.uid, routing_key=self.callsign
                        )
"""
    if old_callsign_bind in source:
        source = source.replace(old_callsign_bind, new_callsign_bind, 1)
    elif new_callsign_bind not in source:
        raise RuntimeError("RabbitMQ callsign DM binding anchor missing")

    old_bound_queue = """                        if {
                            "exchange": "dms",
                            "routing_key": self.callsign,
                            "queue": self.callsign,
                        } not in self.bound_queues:
                            self.bound_queues.append(
                                {
                                    "exchange": "dms",
                                    "routing_key": self.callsign,
                                    "queue": self.callsign,
                                }
                            )
"""
    new_bound_queue = """                        if {
                            "exchange": "dms",
                            "routing_key": self.callsign,
                            "queue": self.uid,
                        } not in self.bound_queues:
                            self.bound_queues.append(
                                {
                                    "exchange": "dms",
                                    "routing_key": self.callsign,
                                    "queue": self.uid,
                                }
                            )
"""
    if old_bound_queue in source:
        source = source.replace(old_bound_queue, new_bound_queue, 1)
    elif new_bound_queue not in source:
        raise RuntimeError("RabbitMQ callsign bound-queue bookkeeping anchor missing")

    old_callsign_consumer = """                        self.rabbit_channel.basic_consume(
                            queue=self.callsign, on_message_callback=self.on_message, auto_ack=True
                        )
"""
    if old_callsign_consumer in source:
        source = source.replace(old_callsign_consumer, "", 1)

    if (
        "queue_declare(queue=self.callsign)" in source
        or 'exchange="dms", queue=self.callsign, routing_key=self.callsign' in source
        or "queue=self.callsign, on_message_callback=self.on_message" in source
    ):
        raise RuntimeError("Shared callsign RabbitMQ queue operations remain after patch")

    return source


def migrate_database() -> None:
    # Import only after the model source has been patched.
    from sqlalchemy import inspect, text

    from opentakserver.app import create_app
    from opentakserver.extensions import db

    app = create_app(cli=True)

    with app.app_context():
        dialect = db.engine.dialect.name
        if dialect != "postgresql":
            raise RuntimeError(
                f"Unsupported database dialect for callsign migration: {dialect}"
            )

        inspector = inspect(db.engine)
        unique_constraints = inspector.get_unique_constraints("euds")
        callsign_constraints = [
            item
            for item in unique_constraints
            if set(item.get("column_names") or []) == {"callsign"}
        ]

        preparer = db.engine.dialect.identifier_preparer
        table_name = preparer.quote("euds")

        try:
            for constraint in callsign_constraints:
                name = constraint.get("name")
                if not name:
                    raise RuntimeError(
                        "Found unnamed UNIQUE constraint on euds.callsign"
                    )
                quoted_name = preparer.quote(name)
                db.session.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"DROP CONSTRAINT {quoted_name}"
                    )
                )

            db.session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_airsoft_euds_callsign ON euds (callsign)"
                )
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        # Re-inspect after DDL to prove uniqueness is gone.
        inspector = inspect(db.engine)
        remaining = [
            item
            for item in inspector.get_unique_constraints("euds")
            if set(item.get("column_names") or []) == {"callsign"}
        ]
        if remaining:
            raise RuntimeError(
                "UNIQUE constraint on euds.callsign still exists after migration"
            )

        if callsign_constraints:
            names = ", ".join(
                str(item.get("name") or "<unnamed>")
                for item in callsign_constraints
            )
            print(f"Dropped UNIQUE euds.callsign constraint(s): {names}")
        else:
            print("UNIQUE euds.callsign constraint already absent.")

        print("Non-unique euds.callsign index is present.")


def main() -> int:
    root = package_root()
    model_path = root / "models" / "EUD.py"
    handler_path = root / "eud_handler" / "EudHandler.py"

    for path in (model_path, handler_path):
        if not path.is_file():
            raise RuntimeError(f"Missing OTS source file: {path}")

    model_source = model_path.read_text(encoding="utf-8")
    handler_source = handler_path.read_text(encoding="utf-8")

    model_patched = patch_model(model_source)
    handler_patched = patch_handler(handler_source)

    backups: list[tuple[Path, Path]] = []

    try:
        if model_patched != model_source:
            saved = backup(model_path, "multi-device-callsign")
            backups.append((model_path, saved))
            model_path.write_text(model_patched, encoding="utf-8")

        if handler_patched != handler_source:
            saved = backup(handler_path, "multi-device-callsign")
            backups.append((handler_path, saved))
            handler_path.write_text(handler_patched, encoding="utf-8")

        py_compile.compile(str(model_path), doraise=True)
        py_compile.compile(str(handler_path), doraise=True)

        migrate_database()

    except Exception:
        for path, saved in reversed(backups):
            shutil.copy2(saved, path)
        raise

    print("Installed multi-device callsign support.")
    print("Each EUD now consumes only its UID RabbitMQ queue.")
    print("Callsign-addressed DMs fan out to every UID queue sharing that callsign.")
    print("Python compile checks passed.")
    for _, saved in backups:
        print(f"Backup: {saved}")
    print("Restart OpenTAKServer before testing the second device.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
