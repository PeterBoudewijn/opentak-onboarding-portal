#!/usr/bin/env python3
"""Permit only safe EUD reassignment to permanent CO accounts.

Arbitrary cross-user EUD transfers remain blocked. The only allowed transfers are:
- a temporary airsoft account (roodNNNN/blauwNNNN) -> CORood/COBlauw
- historical COBLauw -> COBlauw
"""
from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

MARKER = "# AIRSOFT_CO_EUD_REASSIGNMENT_V1"


def package_root() -> Path:
    spec = importlib.util.find_spec("opentakserver")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("Could not locate opentakserver")
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def main() -> int:
    target = package_root() / "blueprints" / "marti_api" / "certificate_enrollment_api.py"
    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        py_compile.compile(str(target), doraise=True)
        print("Controlled CO EUD reassignment patch already installed.")
        return 0

    old = """            if eud.user_id and eud.user_id != user.id:
                logger.warning(
                    "Rejected enrollment for EUD %s owned by another user",
                    uid,
                )
                return jsonify({"error": "EUD UID belongs to another user"}), 403
            if user and not eud.user_id:
                eud.user_id = user.id
                db.session.add(eud)
                db.session.commit()
"""

    new = """            if eud.user_id and eud.user_id != user.id:
                # AIRSOFT_CO_EUD_REASSIGNMENT_V1
                previous_user = eud.user
                previous_username = (
                    str(previous_user.username)
                    if previous_user and previous_user.username
                    else ""
                )
                requested_username = str(user.username or "")

                target_is_co = requested_username.casefold() in ("corood", "coblauw")
                previous_is_temp_game = bool(
                    __import__("re").fullmatch(
                        r"(?:rood|blauw)\\d{4}",
                        previous_username,
                        __import__("re").IGNORECASE,
                    )
                )
                blue_legacy_migration = (
                    requested_username == "COBlauw"
                    and previous_username == "COBLauw"
                )

                if target_is_co and (previous_is_temp_game or blue_legacy_migration):
                    logger.warning(
                        "Reassigning EUD %s from %s to permanent CO %s",
                        uid,
                        previous_username or "<unknown>",
                        requested_username,
                    )
                    eud.user_id = user.id
                    db.session.add(eud)
                    db.session.commit()
                else:
                    logger.warning(
                        "Rejected enrollment for EUD %s owned by %s; requested by %s",
                        uid,
                        previous_username or "<unknown>",
                        requested_username,
                    )
                    return jsonify({"error": "EUD UID belongs to another user"}), 403
            if user and not eud.user_id:
                eud.user_id = user.id
                db.session.add(eud)
                db.session.commit()
"""

    if old not in source:
        raise RuntimeError(
            "Expected hardened EUD ownership block not found; "
            "certificate enrollment security patch must be installed first."
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(target.name + f".bak-co-eud-reassignment-{stamp}")
    shutil.copy2(target, backup)

    try:
        target.write_text(source.replace(old, new, 1), encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(backup, target)
        raise

    print("Installed controlled CO EUD reassignment patch.")
    print(f"Patched: {target}")
    print(f"Backup: {backup}")
    print("Python compile check passed.")
    print("Restart OpenTAKServer before retrying CO enrollment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
