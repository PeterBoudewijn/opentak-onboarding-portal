#!/usr/bin/env python3
"""Clear CO-only device profiles when a handed-over EUD returns to its owner.

Requires the permanent CO handover patch. This upgrade is intentionally separate
so already-patched OpenTAKServer 1.7.13 installations can be upgraded safely.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

MARKER = "# AIRSOFT_CO_DEVICE_RESTORE_CLEANUP_V1"
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
        print("CO device restore cleanup patch already installed.")
        return 0

    if HANDOVER_MARKER not in source:
        raise RuntimeError(
            "Permanent CO device handover patch must be installed before restore cleanup."
        )

    helper_anchor = "\ndef _airsoft_sign_csr(cert_authority, csr_bytes, common_name):\n"
    helper = r'''

# AIRSOFT_CO_DEVICE_RESTORE_CLEANUP_V1
def _airsoft_clear_co_profiles(eud):
    """Remove only game-CO preferences before restoring the normal owner."""
    for preference_key in ("locationTeam", "atakRoleType", "locationCallsign"):
        db.session.query(DeviceProfiles).filter_by(
            preference_key=preference_key,
            eud_uid=eud.uid,
        ).delete(synchronize_session=False)

'''
    if helper_anchor not in source:
        raise RuntimeError(f"CO signing helper anchor missing: {target}")
    source = source.replace(helper_anchor, helper + helper_anchor, 1)

    old_restore = '''                    eud.user_id = user.id
                    eud.last_status = "Disconnected"
                    _airsoft_clear_original_owner(eud)
                    db.session.add(eud)
                    db.session.commit()
'''
    new_restore = '''                    eud.user_id = user.id
                    eud.last_status = "Disconnected"
                    _airsoft_clear_co_profiles(eud)
                    _airsoft_clear_original_owner(eud)
                    db.session.add(eud)
                    db.session.commit()
'''
    if old_restore not in source:
        raise RuntimeError(f"CO restore block anchor missing: {target}")
    source = source.replace(old_restore, new_restore, 1)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = target.with_name(target.name + f".bak-co-restore-cleanup-{stamp}")
    shutil.copy2(target, saved)

    try:
        target.write_text(source, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(saved, target)
        raise

    print("Installed CO device restore cleanup patch.")
    print(f"Patched: {target}")
    print(f"Backup: {saved}")
    print("Python compile check passed.")
    print("Restart OpenTAKServer before testing owner restoration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
