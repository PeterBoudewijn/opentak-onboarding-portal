#!/usr/bin/env python3
"""Patch OTS 1.7.13 to protect reserved GameRed/GameBlue content by hash.

OpenTAKServer's generic /Marti/sync/content endpoint accepts only a SHA-256
hash. Upstream OTS therefore cannot tell from the URL which mission the caller
is trying to read. This patch resolves MissionContent -> Mission associations
from the database and, for GameRed/GameBlue content, requires the mTLS user to
have an enabled membership in at least one matching reserved team group.

It also applies the same authorization rule to the hash-only mission-content
keywords endpoint so a cross-team user cannot modify metadata by knowing a
content hash.

Content that is not associated with GameRed or GameBlue keeps normal upstream
OTS behaviour. Administrators keep access. OUT-only observers are accepted,
matching the existing mission-isolation policy.
"""

from __future__ import annotations

import importlib.util
import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "# AIRSOFT_GAME_CONTENT_ISOLATION_V1"
MISSION_ISOLATION_MARKER = "# AIRSOFT_GAME_MISSION_ISOLATION_V1"


def package_root() -> Path:
    spec = importlib.util.find_spec("opentakserver")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "Could not locate opentakserver. Run with the OTS 1.7.13 venv Python."
        )
    return Path(next(iter(spec.submodule_search_locations))).resolve()


DATA_IMPORT_ANCHOR = "from opentakserver.models.MissionContent import MissionContent\n"

DATA_IMPORTS = """from opentakserver.blueprints.marti_api.marti_api import verify_client_cert
from opentakserver.models.GroupUser import GroupUser
from opentakserver.models.MissionContentMission import MissionContentMission
"""

DATA_BLUEPRINT_ANCHOR = 'data_package_marti_api = Blueprint("data_package_marti_api", __name__)\n'

DATA_HELPER = r'''

# AIRSOFT_GAME_CONTENT_ISOLATION_V1
AIRSOFT_RESERVED_CONTENT_MISSION_GROUPS = {
    "GameRed": "GameRed",
    "GameBlue": "GameBlue",
}


def _airsoft_content_request_user():
    """Resolve the OTS user authenticated by the current mTLS certificate."""
    cert = verify_client_cert()
    if not cert:
        return None

    username = cert.get_subject().commonName
    if not username:
        return None

    return app.security.datastore.find_user(username=username)


def _airsoft_reserved_missions_for_content(content):
    """Return reserved game missions associated with one MissionContent row."""
    if content is None:
        return set()

    return {
        association.mission_name
        for association in db.session.execute(
            db.session.query(MissionContentMission).filter_by(
                mission_content_id=content.id
            )
        ).scalars()
        if association.mission_name in AIRSOFT_RESERVED_CONTENT_MISSION_GROUPS
    }


def _airsoft_user_can_access_reserved_content_missions(user, mission_names):
    """Apply current enabled team memberships to a set of reserved missions."""
    reserved = {
        name
        for name in mission_names
        if name in AIRSOFT_RESERVED_CONTENT_MISSION_GROUPS
    }
    if not reserved:
        return True

    if not user:
        return False

    if user.has_role("administrator"):
        return True

    enabled_group_names = {
        membership.group.name
        for membership in db.session.execute(
            db.session.query(GroupUser).filter_by(
                user_id=user.id,
                enabled=True,
            )
        ).scalars()
        if membership.group
    }

    required_groups = {
        AIRSOFT_RESERVED_CONTENT_MISSION_GROUPS[name]
        for name in reserved
    }
    return bool(enabled_group_names.intersection(required_groups))


def _airsoft_user_can_access_reserved_content(user, content):
    return _airsoft_user_can_access_reserved_content_missions(
        user,
        _airsoft_reserved_missions_for_content(content),
    )

'''

DOWNLOAD_OLD = '''    file_hash = request.args.get("hash")
    file = db.session.execute(db.select(DataPackage).filter_by(hash=file_hash)).first()
    if not file:
        file = db.session.execute(db.select(MissionContent).filter_by(hash=file_hash)).first()
    if not file and request.method == "HEAD":
'''

DOWNLOAD_NEW = '''    file_hash = request.args.get("hash")

    # Mission content is globally addressable by hash in upstream OTS. Resolve
    # its mission association first and enforce reserved GameRed/GameBlue
    # membership before either a DataPackage or MissionContent row can serve
    # the same hash.
    mission_content = db.session.execute(
        db.select(MissionContent).filter_by(hash=file_hash)
    ).first()
    if mission_content:
        content = mission_content[0]
        request_user = _airsoft_content_request_user()
        if not _airsoft_user_can_access_reserved_content(request_user, content):
            username = request_user.username if request_user else "unknown"
            logger.warning(
                "Denied cross-team airsoft content access: user=%s hash=%s",
                username,
                str(file_hash or "")[:16],
            )
            if request.method == "HEAD":
                return "", 403
            return jsonify({"success": False, "error": "Access denied"}), 403

    file = db.session.execute(db.select(DataPackage).filter_by(hash=file_hash)).first()
    if not file:
        file = mission_content
    if not file and request.method == "HEAD":
'''

MISSION_HELPER_ANCHOR = "\\ndef _airsoft_request_mission_name():\\n"

MISSION_HELPER = r'''

# AIRSOFT_GAME_CONTENT_ISOLATION_V1
def _airsoft_user_can_access_reserved_content(user, content):
    """Apply game-mission membership to hash-addressed MissionContent."""
    if content is None:
        return True

    mission_names = {
        association.mission_name
        for association in db.session.execute(
            db.session.query(MissionContentMission).filter_by(
                mission_content_id=content.id
            )
        ).scalars()
        if association.mission_name in AIRSOFT_GAME_MISSION_GROUPS
    }

    if not mission_names:
        return True

    return any(
        _airsoft_user_can_access_game_mission(user, mission_name)
        for mission_name in mission_names
    )

'''

KEYWORDS_OLD = '''    content: MissionContent = content[0]
    current_keywords = content.keywords if content.keywords else []
'''

KEYWORDS_NEW = '''    content: MissionContent = content[0]

    # This route has only a content hash, not a mission name. Protect reserved
    # game content using its MissionContentMission associations.
    request_user = _airsoft_request_user()
    if not _airsoft_user_can_access_reserved_content(request_user, content):
        username = request_user.username if request_user else "unknown"
        logger.warning(
            "Denied cross-team airsoft content metadata update: user=%s hash=%s",
            username,
            str(content_hash or "")[:16],
        )
        return jsonify({"success": False, "error": "Access denied"}), 403

    current_keywords = content.keywords if content.keywords else []
'''


def patch_data_package(source: str) -> str:
    if MARKER in source:
        return source

    patched = source

    if "from opentakserver.blueprints.marti_api.marti_api import verify_client_cert\n" not in patched:
        if DATA_IMPORT_ANCHOR not in patched:
            raise RuntimeError("MissionContent import anchor missing in data package module")
        patched = patched.replace(
            DATA_IMPORT_ANCHOR,
            DATA_IMPORTS + DATA_IMPORT_ANCHOR,
            1,
        )

    if DATA_BLUEPRINT_ANCHOR not in patched:
        raise RuntimeError("data-package blueprint anchor missing")
    patched = patched.replace(
        DATA_BLUEPRINT_ANCHOR,
        DATA_BLUEPRINT_ANCHOR + DATA_HELPER,
        1,
    )

    if DOWNLOAD_OLD not in patched:
        raise RuntimeError("upstream /Marti/sync/content block not found")
    patched = patched.replace(DOWNLOAD_OLD, DOWNLOAD_NEW, 1)

    return patched


def patch_mission(source: str) -> str:
    if MARKER in source:
        return source

    if MISSION_ISOLATION_MARKER not in source:
        raise RuntimeError(
            "Game mission isolation patch must be installed before content isolation."
        )

    patched = source

    if MISSION_HELPER_ANCHOR not in patched:
        raise RuntimeError("mission helper anchor missing")
    patched = patched.replace(
        MISSION_HELPER_ANCHOR,
        MISSION_HELPER + MISSION_HELPER_ANCHOR,
        1,
    )

    if KEYWORDS_OLD not in patched:
        raise RuntimeError("mission-content keywords block not found")
    patched = patched.replace(KEYWORDS_OLD, KEYWORDS_NEW, 1)

    return patched


def write_patched(target: Path, source: str, patched: str, label: str) -> Path | None:
    if patched == source:
        py_compile.compile(str(target), doraise=True)
        print(f"{label} already patched.")
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(target.name + f".bak-airsoft-content-{stamp}")
    shutil.copy2(target, backup)

    try:
        target.write_text(patched, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception:
        shutil.copy2(backup, target)
        raise

    print(f"Patched {label}: {target}")
    print(f"Backup: {backup}")
    return backup


def main() -> int:
    root = package_root()
    data_target = root / "blueprints" / "marti_api" / "data_package_marti_api.py"
    mission_target = root / "blueprints" / "marti_api" / "mission_marti_api.py"

    data_source = data_target.read_text(encoding="utf-8")
    mission_source = mission_target.read_text(encoding="utf-8")

    data_patched = patch_data_package(data_source)
    mission_patched = patch_mission(mission_source)

    # Patch the data-package module first. If the second write fails, restore
    # the first from its timestamped backup so the installer is transactional.
    data_backup = None
    try:
        data_backup = write_patched(
            data_target,
            data_source,
            data_patched,
            "hash download authorization",
        )
        write_patched(
            mission_target,
            mission_source,
            mission_patched,
            "hash metadata authorization",
        )
    except Exception:
        if data_backup and data_backup.exists():
            shutil.copy2(data_backup, data_target)
        raise

    py_compile.compile(str(data_target), doraise=True)
    py_compile.compile(str(mission_target), doraise=True)

    print("Airsoft GameRed/GameBlue hash-content isolation is installed.")
    print("Python compile checks passed.")
    print("Restart OpenTAKServer before testing content downloads.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
