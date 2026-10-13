#!/usr/bin/env python3
"""Allow multiple TAK devices to share one callsign safely on OTS 1.7.13.

Upstream OTS 1.7.13 makes EUD.callsign UNIQUE and also uses that column as the
target of mission_invitations.callsign. That schema prevents two physical TAK
devices from using the same account/callsign at the same time.

This patch:
1. removes the ORM uniqueness flag from EUD.callsign;
2. makes MissionInvitation.callsign a plain string instead of an FK to
   EUD.callsign (upstream invitation lookup already compares the stored string);
3. removes the database FK that depends on the callsign UNIQUE constraint;
4. removes the database UNIQUE constraint and adds a normal callsign index;
5. routes callsign-addressed DMs into every matching per-device UID queue rather
   than using one shared RabbitMQ callsign queue.

The migration is intentionally PostgreSQL-only because this production OTS
deployment uses PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER_MODEL = "# AIRSOFT_MULTI_DEVICE_CALLSIGN_MODEL_V1"
MARKER_INVITATION = "# AIRSOFT_MULTI_DEVICE_CALLSIGN_INVITATION_V1"
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


def patch_eud_model(source: str) -> str:
    if MARKER_MODEL in source:
        return source

    old = (
        "    callsign: Mapped[str] = mapped_column("
        "String(255), nullable=True, unique=True)\n"
    )
    new = (
        f"    {MARKER_MODEL}\n"
        "    # Physical TAK devices may intentionally share one account/callsign.\n"
        "    callsign: Mapped[str] = mapped_column("
        "String(255), nullable=True, unique=False)\n"
    )

    if old not in source:
        raise RuntimeError("EUD.callsign unique model anchor missing")

    return source.replace(old, new, 1)


def patch_invitation_model(source: str) -> str:
    if MARKER_INVITATION in source:
        return source

    old_column = """    callsign: Mapped[str] = mapped_column(
        String(255), ForeignKey("euds.callsign", ondelete="CASCADE"), nullable=True
    )
"""
    new_column = """    # AIRSOFT_MULTI_DEVICE_CALLSIGN_INVITATION_V1
    # Callsigns are not unique across physical devices. Invitations already
    # store and query the callsign string directly, so this must not be an FK.
    callsign: Mapped[str] = mapped_column(String(255), nullable=True)
"""

    if old_column not in source:
        raise RuntimeError("MissionInvitation.callsign foreign-key anchor missing")
    source = source.replace(old_column, new_column, 1)

    old_relationship = (
        '    eud_callsign = relationship("EUD", '
        "foreign_keys=[callsign], uselist=False)\n"
    )
    if old_relationship not in source:
        raise RuntimeError(
            "MissionInvitation.eud_callsign relationship anchor missing"
        )

    return source.replace(old_relationship, "", 1)


def patch_handler(source: str) -> str:
    if MARKER_HANDLER in source:
        return source

    # The production handler is already modified by the game-team hardening,
    # so patch only the individual callsign-queue operations.
    callsign_declare = (
        "                    self.rabbit_channel.queue_declare("
        "queue=self.callsign)\n"
    )
    uid_declare = (
        "                    self.rabbit_channel.queue_declare("
        "queue=self.uid)\n"
    )

    if callsign_declare in source:
        source = source.replace(
            callsign_declare,
            "                    "
            + MARKER_HANDLER
            + "\n"
            + "                    # Shared callsigns use only per-device UID queues.\n",
            1,
        )
    elif uid_declare in source:
        source = source.replace(
            uid_declare,
            "                    " + MARKER_HANDLER + "\n" + uid_declare,
            1,
        )
    else:
        raise RuntimeError("RabbitMQ UID queue declaration anchor missing")

    old_bind = """                        self.rabbit_channel.queue_bind(
                            exchange="dms", queue=self.callsign, routing_key=self.callsign
                        )
"""
    new_bind = """                        self.rabbit_channel.queue_bind(
                            exchange="dms", queue=self.uid, routing_key=self.callsign
                        )
"""

    if old_bind in source:
        source = source.replace(old_bind, new_bind, 1)
    elif new_bind not in source:
        raise RuntimeError("RabbitMQ callsign DM binding anchor missing")

    old_bookkeeping = """                        if {
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
    new_bookkeeping = """                        if {
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

    if old_bookkeeping in source:
        source = source.replace(old_bookkeeping, new_bookkeeping, 1)
    elif new_bookkeeping not in source:
        raise RuntimeError("RabbitMQ callsign bookkeeping anchor missing")

    old_consumer = """                        self.rabbit_channel.basic_consume(
                            queue=self.callsign, on_message_callback=self.on_message, auto_ack=True
                        )
"""
    if old_consumer in source:
        source = source.replace(old_consumer, "", 1)

    leftovers = (
        "queue_declare(queue=self.callsign)",
        'exchange="dms", queue=self.callsign, routing_key=self.callsign',
        "queue=self.callsign, on_message_callback=self.on_message",
    )
    if any(item in source for item in leftovers):
        raise RuntimeError("Shared callsign RabbitMQ operations remain after patch")

    return source


def migrate_database() -> None:
    # Import OTS only after the ORM source files have been patched.
    from sqlalchemy import inspect, text

    from opentakserver.app import create_app
    from opentakserver.extensions import db

    app = create_app(cli=True)

    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            raise RuntimeError(
                "Multi-device callsign migration requires PostgreSQL"
            )

        inspector = inspect(db.engine)

        invitation_fks = [
            item
            for item in inspector.get_foreign_keys("mission_invitations")
            if item.get("referred_table") == "euds"
            and set(item.get("constrained_columns") or []) == {"callsign"}
            and set(item.get("referred_columns") or []) == {"callsign"}
        ]

        callsign_unique = [
            item
            for item in inspector.get_unique_constraints("euds")
            if set(item.get("column_names") or []) == {"callsign"}
        ]

        preparer = db.engine.dialect.identifier_preparer
        invitations_table = preparer.quote("mission_invitations")
        euds_table = preparer.quote("euds")

        try:
            # The FK must be removed before PostgreSQL allows the UNIQUE
            # constraint/index it depends on to be removed.
            for foreign_key in invitation_fks:
                name = foreign_key.get("name")
                if not name:
                    raise RuntimeError(
                        "Found unnamed callsign FK on mission_invitations"
                    )
                db.session.execute(
                    text(
                        f"ALTER TABLE {invitations_table} "
                        f"DROP CONSTRAINT {preparer.quote(name)}"
                    )
                )

            for constraint in callsign_unique:
                name = constraint.get("name")
                if not name:
                    raise RuntimeError(
                        "Found unnamed UNIQUE constraint on euds.callsign"
                    )
                db.session.execute(
                    text(
                        f"ALTER TABLE {euds_table} "
                        f"DROP CONSTRAINT {preparer.quote(name)}"
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

        # Prove the schema is in the intended state before returning success.
        inspector = inspect(db.engine)

        remaining_fks = [
            item
            for item in inspector.get_foreign_keys("mission_invitations")
            if item.get("referred_table") == "euds"
            and set(item.get("constrained_columns") or []) == {"callsign"}
            and set(item.get("referred_columns") or []) == {"callsign"}
        ]
        if remaining_fks:
            raise RuntimeError(
                "mission_invitations.callsign FK still exists after migration"
            )

        remaining_unique = [
            item
            for item in inspector.get_unique_constraints("euds")
            if set(item.get("column_names") or []) == {"callsign"}
        ]
        if remaining_unique:
            raise RuntimeError(
                "UNIQUE constraint on euds.callsign still exists after migration"
            )

        nonunique_indexes = [
            item
            for item in inspector.get_indexes("euds")
            if set(item.get("column_names") or []) == {"callsign"}
            and not item.get("unique")
        ]
        if not nonunique_indexes:
            raise RuntimeError("Non-unique euds.callsign index is missing")

        if invitation_fks:
            print(
                "Dropped mission-invitation callsign FK(s): "
                + ", ".join(str(item.get("name")) for item in invitation_fks)
            )
        else:
            print("Mission-invitation callsign FK already absent.")

        if callsign_unique:
            print(
                "Dropped UNIQUE euds.callsign constraint(s): "
                + ", ".join(str(item.get("name")) for item in callsign_unique)
            )
        else:
            print("UNIQUE euds.callsign constraint already absent.")

        print("Non-unique euds.callsign index is present.")


def main() -> int:
    root = package_root()
    eud_model = root / "models" / "EUD.py"
    invitation_model = root / "models" / "MissionInvitation.py"
    handler = root / "eud_handler" / "EudHandler.py"

    for path in (eud_model, invitation_model, handler):
        if not path.is_file():
            raise RuntimeError(f"Missing OTS source file: {path}")

    originals = {
        eud_model: eud_model.read_text(encoding="utf-8"),
        invitation_model: invitation_model.read_text(encoding="utf-8"),
        handler: handler.read_text(encoding="utf-8"),
    }

    patched = {
        eud_model: patch_eud_model(originals[eud_model]),
        invitation_model: patch_invitation_model(originals[invitation_model]),
        handler: patch_handler(originals[handler]),
    }

    backups: list[tuple[Path, Path]] = []

    try:
        for path in (eud_model, invitation_model, handler):
            if patched[path] == originals[path]:
                continue
            saved = backup(path, "multi-device-callsign")
            backups.append((path, saved))
            path.write_text(patched[path], encoding="utf-8")

        for path in (eud_model, invitation_model, handler):
            py_compile.compile(str(path), doraise=True)

        migrate_database()

    except Exception:
        for path, saved in reversed(backups):
            shutil.copy2(saved, path)
        raise

    print("Installed multi-device callsign support.")
    print("Mission invitations no longer require a unique EUD callsign.")
    print("Each EUD consumes only its own UID RabbitMQ queue.")
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
