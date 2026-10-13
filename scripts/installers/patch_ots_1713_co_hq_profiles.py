#!/usr/bin/env python3
"""Make OTS 1.7.13 apply CO team/callsign/HQ profiles at enrollment.

ATAK creates the EUD row during certificate enrollment and then requests its
connection profile. Creating the per-EUD profile here makes the desired
locationTeam, locationCallsign and atakRoleType available on that first
connection instead of waiting for the portal's periodic profile guard.

The comparison is case-insensitive so the historical OTS spelling COBLauw
continues to work while the user-facing callsign is COBlauw.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
from datetime import datetime
from pathlib import Path

MARKER = "# AIRSOFT_CO_HQ_PROFILE_ON_ENROLLMENT_V1"


def package_root() -> Path:
    spec = importlib.util.find_spec("opentakserver")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "Could not locate opentakserver. Run this with the OTS 1.7.13 venv Python."
        )
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(path.name + f".bak-co-hq-profile-{stamp}")
    shutil.copy2(path, dest)
    return dest


def patch(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        return False

    import_anchor = "from opentakserver.models.EUD import EUD\n"
    if import_anchor not in source:
        raise RuntimeError(f"EUD import anchor missing: {path}")
    source = source.replace(
        import_anchor,
        import_anchor + "from opentakserver.models.DeviceProfiles import DeviceProfiles\n",
        1,
    )

    function_anchor = "\n\n# require basic auth\n"
    if function_anchor not in source:
        raise RuntimeError(f"basic auth anchor missing: {path}")

    helper = r'''

# AIRSOFT_CO_HQ_PROFILE_ON_ENROLLMENT_V1
def _airsoft_apply_co_profile(username, eud):
    """Create/update the per-device game profile for permanent CO accounts."""
    normalized = str(username or "").casefold()
    if normalized == "corood":
        team_color = "Red"
        callsign = "CORood"
    elif normalized == "coblauw":
        team_color = "Blue"
        callsign = "COBlauw"
    else:
        return

    publish_time = datetime.datetime.now(datetime.timezone.utc)
    for preference_key, preference_value in (
        ("locationTeam", team_color),
        ("atakRoleType", "HQ"),
        ("locationCallsign", callsign),
    ):
        profile = db.session.query(DeviceProfiles).filter_by(
            preference_key=preference_key,
            eud_uid=eud.uid,
        ).first()
        if not profile:
            profile = DeviceProfiles(
                preference_key=preference_key,
                eud_uid=eud.uid,
            )

        profile.preference_value = preference_value
        profile.value_class = "class java.lang.String"
        profile.enrollment = False
        profile.connection = True
        profile.active = True
        profile.publish_time = publish_time
        db.session.add(profile)

    db.session.commit()
'''
    source = source.replace(function_anchor, helper + function_anchor, 1)

    certificate_anchor = '''        try:
            certificate = Certificate()
'''
    if certificate_anchor not in source:
        raise RuntimeError(f"certificate creation anchor missing: {path}")

    profile_call = '''        if uid and user:
            _airsoft_apply_co_profile(user.username, eud)

        try:
            certificate = Certificate()
'''
    source = source.replace(certificate_anchor, profile_call, 1)

    path.write_text(source, encoding="utf-8")
    return True


def main() -> int:
    root = package_root()
    target = root / "blueprints" / "marti_api" / "certificate_enrollment_api.py"
    if not target.is_file():
        raise RuntimeError(f"Missing OTS source file: {target}")

    saved = None
    try:
        if MARKER not in target.read_text(encoding="utf-8"):
            saved = backup(target)
        changed = patch(target)
        py_compile.compile(str(target), doraise=True)
    except Exception:
        if saved:
            shutil.copy2(saved, target)
        raise

    if changed:
        print("Installed CO HQ enrollment profile patch.")
        print(f"Patched: {target}")
    else:
        print("CO HQ enrollment profile patch already installed.")

    print("Python compile check passed.")
    if saved:
        print(f"Backup: {saved}")
    print("Restart OpenTAKServer before testing a new CO enrollment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
