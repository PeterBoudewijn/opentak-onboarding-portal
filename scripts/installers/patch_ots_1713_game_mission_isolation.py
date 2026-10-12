#!/usr/bin/env python3
"""Patch OpenTAKServer 1.7.13 to hard-isolate the GameRed/GameBlue missions.

The patch is deliberately narrow:
- GameRed requires an enabled membership in OTS group GameRed.
- GameBlue requires an enabled membership in OTS group GameBlue.
- Membership direction may be IN or OUT so watch-only observers can read both
  game missions without publishing their own PLI into the game groups.
- OTS administrators keep access to both missions.
- The generic mission list is fixed so non-admin users only see missions that
  belong to one of their enabled groups; users with no groups only see __ANON__.
- Generic invitation listings hide reserved game-mission invitations from
  users who do not have the matching group.

The script is idempotent, creates a timestamped backup, and byte-compiles the
patched module before reporting success.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path


MARKER = "# AIRSOFT_GAME_MISSION_ISOLATION_V1"

BLUEPRINT_ANCHOR = 'mission_marti_api = Blueprint("mission_marti_api", __name__)\n'

GUARD_BLOCK = r'''
# AIRSOFT_GAME_MISSION_ISOLATION_V1
AIRSOFT_GAME_MISSION_GROUPS = {
    "GameRed": "GameRed",
    "GameBlue": "GameBlue",
}


def _airsoft_request_mission_name():
    """Resolve a reserved game mission from the current Marti request."""
    view_args = request.view_args or {}

    mission_name = view_args.get("mission_name")
    if mission_name in AIRSOFT_GAME_MISSION_GROUPS:
        return mission_name

    mission_guid = view_args.get("mission_guid")
    if mission_guid:
        mission = db.session.execute(
            db.session.query(Mission).filter_by(guid=mission_guid)
        ).scalar()
        if mission and mission.name in AIRSOFT_GAME_MISSION_GROUPS:
            return mission.name

    # The mission log creation route carries the mission name in JSON rather
    # than in the URL, so include it in the same guard.
    if request.path.endswith("/missions/logs/entries") and request.is_json:
        payload = request.get_json(silent=True) or {}
        mission_names = payload.get("missionNames") or []
        if mission_names and mission_names[0] in AIRSOFT_GAME_MISSION_GROUPS:
            return mission_names[0]

    return None


def _airsoft_request_user():
    """Resolve the OTS user authenticated by the mTLS client certificate."""
    cert = verify_client_cert()
    if not cert:
        return None

    username = cert.get_subject().commonName
    if not username:
        return None

    return app.security.datastore.find_user(username=username)


def _airsoft_user_can_access_game_mission(user, mission_name):
    """Check the current enabled OTS group membership for a game mission."""
    required_group = AIRSOFT_GAME_MISSION_GROUPS.get(mission_name)
    if not required_group:
        return True

    if not user:
        return False

    if user.has_role("administrator"):
        return True

    memberships = db.session.execute(
        db.session.query(GroupUser).filter_by(
            user_id=user.id,
            enabled=True,
        )
    ).scalars()

    return any(
        membership.group
        and membership.group.name == required_group
        for membership in memberships
    )


@mission_marti_api.before_request
def _airsoft_enforce_game_mission_group():
    """Deny every mission-specific GameRed/GameBlue route cross-team."""
    mission_name = _airsoft_request_mission_name()
    if not mission_name:
        return None

    user = _airsoft_request_user()
    if _airsoft_user_can_access_game_mission(user, mission_name):
        return None

    username = user.username if user else "unknown"
    logger.warning(
        "Denied cross-team airsoft mission access: user=%s mission=%s",
        username,
        mission_name,
    )
    return (
        jsonify(
            {
                "success": False,
                "error": "Access denied for this game mission",
            }
        ),
        403,
    )

'''

LIST_OLD = '''        # Let admins see all missions
        if not user.has_role("administrator"):
            group_filters = []
            groups = db.session.execute(
                db.session.query(GroupUser).filter_by(user_id=user.id, direction=Group.IN)
            ).scalars()
            for group in groups:
                group_filters.append(GroupMission.group_id == group.group.id)
            if group_filters:
                query = query.outerjoin(GroupMission).where(or_(*group_filters))
            else:
                # If a user isn't in a group, only show them __ANON__ missions
                query.outerjoin(GroupMission).where(GroupMission.group_id == 1)
'''

LIST_NEW = '''        # Let admins see all missions. Non-admin users may only see
        # missions associated with one of their currently enabled memberships.
        # Both IN and OUT count for mission visibility; OUT-only is how the
        # airsoft GameMaster can observe both missions without publishing PLI.
        if not user.has_role("administrator"):
            memberships = db.session.execute(
                db.session.query(GroupUser).filter_by(
                    user_id=user.id,
                    enabled=True,
                )
            ).scalars()
            group_ids = {
                membership.group_id
                for membership in memberships
                if membership.group_id is not None
            }

            if group_ids:
                query = (
                    query.outerjoin(GroupMission)
                    .where(GroupMission.group_id.in_(group_ids))
                    .distinct(Mission.name)
                )
            else:
                # No enabled groups: only __ANON__ missions are visible.
                query = (
                    query.outerjoin(GroupMission)
                    .where(GroupMission.group_id == 1)
                    .distinct(Mission.name)
                )
'''

INVITATIONS_OLD = '''    invitations = db.session.execute(query).all()

    for invitation in invitations:
        response["data"].append(invitation[0].to_marti_json())

    return jsonify(response)
'''

INVITATIONS_NEW = '''    invitations = db.session.execute(query).all()

    # Mission-specific URLs are already handled by the before_request guard.
    # The generic /missions/invitations endpoints need response filtering so
    # they cannot leak GameRed invitations to blue users (or vice versa).
    request_user = _airsoft_request_user()

    for invitation in invitations:
        invitation_obj = invitation[0]
        mission = invitation_obj.mission
        if (
            mission
            and mission.name in AIRSOFT_GAME_MISSION_GROUPS
            and not _airsoft_user_can_access_game_mission(
                request_user,
                mission.name,
            )
        ):
            continue

        response["data"].append(invitation_obj.to_marti_json())

    return jsonify(response)
'''


def locate_module(module_name: str) -> Path:
    """Locate the target source without importing mission_marti_api.

    importlib.find_spec() on the full nested module imports parent packages.
    In OTS 1.7.13 that can initialize SQLAlchemy models before the Flask app
    has configured the db extension, which raises errors such as
    "'NoneType' object has no attribute 'relationship'".
    """
    package_spec = importlib.util.find_spec("opentakserver")
    if (
        package_spec is None
        or not package_spec.submodule_search_locations
    ):
        raise RuntimeError(
            "Could not locate the opentakserver package. "
            "Run this script with the OpenTAKServer 1.7.13 virtualenv Python."
        )

    package_root = Path(next(iter(package_spec.submodule_search_locations))).resolve()

    if module_name != "opentakserver.blueprints.marti_api.mission_marti_api":
        raise RuntimeError(f"Unsupported module: {module_name}")

    target = package_root / "blueprints" / "marti_api" / "mission_marti_api.py"
    if not target.is_file():
        raise RuntimeError(f"Could not locate {module_name}: {target}")

    return target


def patch_source(source: str) -> str:
    patched = source

    if MARKER not in patched:
        if BLUEPRINT_ANCHOR not in patched:
            raise RuntimeError("Could not find mission Marti blueprint insertion point.")
        patched = patched.replace(
            BLUEPRINT_ANCHOR,
            BLUEPRINT_ANCHOR + GUARD_BLOCK,
            1,
        )

    if LIST_NEW not in patched:
        if LIST_OLD not in patched:
            raise RuntimeError("Expected OTS 1.7.13 mission-list group filter not found.")
        patched = patched.replace(LIST_OLD, LIST_NEW, 1)

    if INVITATIONS_NEW not in patched:
        if INVITATIONS_OLD not in patched:
            raise RuntimeError("Expected OTS 1.7.13 invitation response block not found.")
        patched = patched.replace(INVITATIONS_OLD, INVITATIONS_NEW, 1)

    return patched


def main() -> int:
    target = locate_module("opentakserver.blueprints.marti_api.mission_marti_api")
    source = target.read_text(encoding="utf-8")
    patched = patch_source(source)

    print(f"mission_marti_api: {target}")

    if patched == source:
        print("Airsoft mission isolation patch is already installed; no changes made.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(target.name + f".bak-airsoft-mission-{stamp}")
    shutil.copy2(target, backup)

    try:
        target.write_text(patched, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(backup, target)
        raise

    print(f"Backup: {backup}")
    print("Installed airsoft mission isolation patch; Python compile check passed.")
    print("Restart the OpenTAKServer service before testing.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
