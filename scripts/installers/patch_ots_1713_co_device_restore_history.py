#!/usr/bin/env python3
"""Allow restoration from CO using trustworthy EUD certificate history.

Devices that became CORood/COBlauw before the handover bookkeeping patch do not
have the airsoftOriginalUsername marker. OpenTAKServer still has their previous
certificate rows, however. For these migrated devices, allow a return from a CO
identity only to the most recent non-CO certificate identity recorded for the
same EUD UID.

If the explicit airsoftOriginalUsername marker exists, that remains authoritative.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

MARKER = "# AIRSOFT_CO_DEVICE_RESTORE_HISTORY_V1"
HANDOVER_MARKER = "# AIRSOFT_CO_DEVICE_HANDOVER_V2"


def package_root() -> Path:
    spec = importlib.util.find_spec("opentakserver")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "Could not locate opentakserver. Run with the OTS 1.7.13 venv Python."
        )
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def main() -> int:
    target = package_root() / "blueprints" / "marti_api" / "certificate_enrollment_api.py"
    source = target.read_text(encoding="utf-8")

    if MARKER in source:
        py_compile.compile(str(target), doraise=True)
        print("CO device restore-history patch already installed.")
        return 0

    if HANDOVER_MARKER not in source:
        raise RuntimeError(
            "Permanent CO device handover patch must be installed before restore history."
        )

    old = '''def _airsoft_restore_allowed(eud, requested_username):
    current_username = eud.user.username if eud.user else ""
    if not _airsoft_is_co_username(current_username):
        return False

    profile = _airsoft_original_owner_profile(eud)
    if not profile or not profile.preference_value:
        return False

    return str(profile.preference_value).casefold() == str(requested_username or "").casefold()
'''

    new = '''def _airsoft_restore_allowed(eud, requested_username):
    # AIRSOFT_CO_DEVICE_RESTORE_HISTORY_V1
    current_username = eud.user.username if eud.user else ""
    requested_username = str(requested_username or "")
    if not _airsoft_is_co_username(current_username):
        return False
    if not requested_username or _airsoft_is_co_username(requested_username):
        return False

    # New handovers explicitly remember the normal owner. This is authoritative.
    profile = _airsoft_original_owner_profile(eud)
    if profile and profile.preference_value:
        return (
            str(profile.preference_value).casefold()
            == requested_username.casefold()
        )

    # Migration path for devices that became CO before owner bookkeeping was
    # installed. OTS already records certificate history per EUD. Only the most
    # recent non-CO certificate identity may reclaim the device.
    certificates = (
        db.session.query(Certificate)
        .filter(Certificate.eud_uid == eud.uid)
        .order_by(Certificate.id.desc())
        .all()
    )
    for certificate in certificates:
        historical_username = str(certificate.common_name or "")
        if not historical_username or _airsoft_is_co_username(historical_username):
            continue

        allowed = historical_username.casefold() == requested_username.casefold()
        if allowed:
            logger.warning(
                "Allowing legacy CO device restore for EUD %s to historical owner %s",
                eud.uid,
                requested_username,
            )
        return allowed

    return False
'''

    if old not in source:
        raise RuntimeError(f"CO restore helper anchor missing: {target}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = target.with_name(target.name + f".bak-co-restore-history-{stamp}")
    shutil.copy2(target, saved)

    try:
        target.write_text(source.replace(old, new, 1), encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(saved, target)
        raise

    print("Installed CO device restore-history patch.")
    print(f"Patched: {target}")
    print(f"Backup: {saved}")
    print("Python compile check passed.")
    print("Restart OpenTAKServer before retrying normal-owner enrollment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
