"""
Airsoft game mode API.

Security model:
- GameRed and GameBlue are logical team labels and OTS group names.
- Every generated game session uses a unique, human-readable OTS username
  (for example rood4821). Old client certificates therefore remain tied
  to an account that is never reactivated and is deleted after disconnect.
- Each session user belongs exclusively to its own team group (IN + OUT).
- Red/blue game leaders only see their own team.
- Only the portal administrator can close all tracked sessions.
- ATAK enrollment tokens expire at the next 23:00 Europe/Amsterdam,
  capped at 24 real hours.
- Closed temporary OTS users are deleted after their EUDs have disconnected.
- GameRed and GameBlue each have a group-isolated mission that is replaced daily at 23:00.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hmac
import json
import os
import secrets
import string
import threading

from flask import current_app, jsonify, request
from flask_jwt_extended import create_access_token, create_refresh_token, get_jwt, jwt_required

from app.api_v1 import api_v1
from app.extensions import scheduler
from app.ots import otsClient
from app.rbac import get_user_roles


GAME_TZ = ZoneInfo("Europe/Amsterdam")
GAME_TEAMS = {
    "red": {
        "label": "GameRed",
        "group": "GameRed",
        "team_color": "Red",
        "leader_role": "game_red_leader",
        "mission": "GameRed",
        "mission_description": "Dagelijkse airsoftmissie voor Team Rood",
    },
    "blue": {
        "label": "GameBlue",
        "group": "GameBlue",
        "team_color": "Blue",
        "leader_role": "game_blue_leader",
        "mission": "GameBlue",
        "mission_description": "Dagelijkse airsoftmissie voor Team Blauw",
    },
}
_STATE_LOCK = threading.Lock()


def _random_password(length=24):
    """Strong internal-only password used when revoking/closing accounts."""
    alphabet = string.ascii_letters + string.digits + "!#$%*_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


GAME_PASSWORD_STREETS = (
    "Acacialaan", "Beatrixstraat", "Beukenlaan", "Boslaan", "Brinkstraat",
    "Burgemeesterstraat", "Dahliastraat", "Dennenlaan", "Dorpsstraat",
    "Eikenlaan", "Emmastraat", "Esdoornlaan", "Goudsbloemstraat",
    "Havenstraat", "Heidestraat", "Herenstraat", "Iepenlaan", "Julianastraat",
    "Kerklaan", "Kerkstraat", "Korenstraat", "Korteweg", "Langeweg",
    "Leliestraat", "Lindenlaan", "Margrietstraat", "Marktstraat",
    "Meidoornlaan", "Molenstraat", "Molenweg", "Nieuwstraat", "Noordstraat",
    "Oranjestraat", "Parklaan", "Pastoorstraat", "Pleinweg", "Prinsesstraat",
    "Rietstraat", "Rozenstraat", "Schoolstraat", "Spoorlaan", "Stationsstraat",
    "Veldstraat", "Vijverlaan", "Wilhelminastraat", "Zandstraat", "Zuidstraat",
    "Akkerweg", "Berkenlaan", "Bloemenstraat", "Boomgaardweg", "Boterstraat",
    "Bremstraat", "Brugstraat", "Dijkstraat", "Duinweg", "Griendweg",
    "Groenestraat", "Hoefstraat", "Kastanjelaan", "Klaverstraat", "Kloosterstraat",
    "Korenbloemstraat", "Kruisstraat", "Lijsterlaan", "Madeliefstraat",
    "Meistraat", "Middenweg", "Nieuweweg", "Oosterstraat", "Patrijslaan",
    "Populierenlaan", "Rivierstraat", "Singelstraat", "Sparrenlaan",
    "Torenstraat", "Tulpenstraat", "Valkenlaan", "Veerstraat", "Waterstraat",
    "Weidestraat", "Weststraat", "Zonnebloemstraat", "Zwaluwlaan",
)


def _human_game_password():
    """Easy one-day password: a Dutch-style street name plus house number."""
    street = secrets.choice(GAME_PASSWORD_STREETS)
    house_number = secrets.randbelow(90) + 10
    return f"{street}{house_number}"


def _new_session_username(team_key):
    """Human-readable temporary username; uniqueness is verified by OTS."""
    prefix = "rood" if team_key == "red" else "blauw"
    # OTS 1.7.13 usernames allow letters, numbers, underscores and periods.
    # Four digits are easy to read/type; create_user() below retries collisions.
    suffix = secrets.randbelow(9000) + 1000
    return f"{prefix}{suffix}"


def _next_cutoff(now=None):
    """Return the earliest of next local 23:00 or exactly 24 real hours."""
    now = now or datetime.now(GAME_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=GAME_TZ)
    else:
        now = now.astimezone(GAME_TZ)

    cutoff = now.replace(hour=23, minute=0, second=0, microsecond=0)
    if now >= cutoff:
        cutoff += timedelta(days=1)

    # UTC-timeline cap keeps the duration <=24 real hours across DST fallback.
    hard_limit = (now.astimezone(timezone.utc) + timedelta(hours=24)).astimezone(GAME_TZ)
    return cutoff if cutoff.timestamp() <= hard_limit.timestamp() else hard_limit


def _state_path():
    configured = current_app.config.get("GAME_STATE_FILE", "instance/game_state.json")
    if os.path.isabs(configured):
        return configured
    project_root = os.path.dirname(current_app.root_path)
    return os.path.join(project_root, configured)


def _empty_state():
    return {
        "version": 1,
        "teams": {
            team_key: {"current": None, "sessions": []}
            for team_key in GAME_TEAMS
        },
    }


def _load_state_unlocked():
    path = _state_path()
    if not os.path.exists(path):
        return _empty_state()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        current_app.logger.exception("Could not read airsoft game state")
        return _empty_state()

    state.setdefault("version", 1)
    state.setdefault("teams", {})
    for team_key in GAME_TEAMS:
        state["teams"].setdefault(team_key, {"current": None, "sessions": []})
        state["teams"][team_key].setdefault("current", None)
        state["teams"][team_key].setdefault("sessions", [])
        for session in state["teams"][team_key]["sessions"]:
            session.setdefault("deletedAt", None)
            session.setdefault("deletePendingReason", None)
    return state


def _save_state_unlocked(state):
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


def _read_state():
    with _STATE_LOCK:
        return _load_state_unlocked()


def _response_payload(response):
    if not isinstance(response, dict):
        return {}
    payload = response.get("response", response)
    return payload if isinstance(payload, dict) else {}


def _response_results(response):
    payload = _response_payload(response)
    results = payload.get("results", [])
    return results if isinstance(results, list) else []


def _can_access_team(team_key):
    roles = set(get_user_roles())
    if "administrator" in roles:
        return True
    team = GAME_TEAMS.get(team_key)
    if not team or team["leader_role"] not in roles:
        return False
    return _qr_session_valid_for_team(team_key)


def _require_team_access(team_key):
    if team_key not in GAME_TEAMS:
        return jsonify({"error": "Unknown game team"}), 404
    if not _can_access_team(team_key):
        return jsonify({"error": "Access denied for this game team"}), 403
    return None


def _require_game_admin():
    if "administrator" not in set(get_user_roles()):
        return jsonify({"error": "Administrator role required"}), 403
    return None


def _co_qr_db_key(team_key, suffix):
    return f"game_co_qr_{suffix}_{team_key}"


def _co_qr_token(team_key):
    from app.models import SystemSettingsModel

    stored = SystemSettingsModel.get_setting(_co_qr_db_key(team_key, "token"), "")
    if stored:
        return str(stored).strip()

    config_key = (
        "GAME_CO_QR_TOKEN_RED"
        if team_key == "red"
        else "GAME_CO_QR_TOKEN_BLUE"
    )
    return str(current_app.config.get(config_key, "") or "").strip()


def _co_qr_version(team_key):
    from app.models import SystemSettingsModel

    value = SystemSettingsModel.get_setting(_co_qr_db_key(team_key, "version"), "0")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rotate_co_qr_token(team_key):
    from app.models import SystemSettingsModel

    token = secrets.token_urlsafe(32)
    version = _co_qr_version(team_key) + 1

    token_setting = SystemSettingsModel.set_setting(
        _co_qr_db_key(team_key, "token"),
        token,
        category="game",
        description=f"Permanent CO QR bearer token for Team {team_key}",
    )
    version_setting = SystemSettingsModel.set_setting(
        _co_qr_db_key(team_key, "version"),
        str(version),
        category="game",
        description=f"CO QR session version for Team {team_key}",
    )
    if not token_setting or not version_setting:
        raise RuntimeError("Could not save CO QR token")
    return token, version


def _qr_session_valid_for_team(team_key):
    """Normal logins remain valid; QR logins must match the current QR version."""
    try:
        claims = get_jwt()
    except Exception:
        return True

    qr_team = claims.get("game_qr_team")
    if not qr_team:
        return True

    if qr_team != team_key:
        return False

    try:
        token_version = int(claims.get("game_qr_version", -1))
    except (TypeError, ValueError):
        return False
    return token_version == _co_qr_version(team_key)


def _co_qr_public_url(team_key, token=None):
    token = token if token is not None else _co_qr_token(team_key)
    if not token:
        return None
    public_base = request.host_url.rstrip("/") + "/game"
    return f"{public_base}/co/{team_key}#{token}"


def _co_portal_username(team_key):
    config_key = (
        "GAME_CO_PORTAL_USERNAME_RED"
        if team_key == "red"
        else "GAME_CO_PORTAL_USERNAME_BLUE"
    )
    return str(current_app.config.get(config_key, "") or "").strip()


def _ensure_co_qr_user(team_key):
    """Use the permanent CO identity for an admin-managed QR login."""
    from app.models import db, UserModel, UserRoleModel

    team = GAME_TEAMS[team_key]
    username = _co_portal_username(team_key)
    if not username:
        raise RuntimeError(f"CO portal username is not configured for team {team_key}")

    callsign = "CO Rood" if team_key == "red" else "CO Blauw"
    email = f"{username.lower()}@game.invalid"

    role = UserRoleModel.get_role_by_name(team["leader_role"])
    if not role:
        role = UserRoleModel.create_role(
            team["leader_role"],
            f"Access only to the {team_key} airsoft game portal",
            f"Game Leader {team_key.title()}",
        )
    if isinstance(role, dict):
        raise RuntimeError(f"Could not create game role {team['leader_role']}")

    # Portal users are normalized to lowercase by UserModel.create_user().
    # Find an existing CO identity case-insensitively so CORood/COBLauw map to
    # the same local account used by a normal portal login.
    user = UserModel.query.filter(
        db.func.lower(UserModel.username) == username.lower()
    ).first()
    if not user:
        user = UserModel.create_user(
            username=username,
            email=email,
            callsign=callsign,
            roles=[role],
        )
    if isinstance(user, dict):
        raise RuntimeError(f"Could not create/find CO portal user {username}")

    # Preserve the user's normal database roles. get_user_roles() limits a
    # QR-derived session to only game_<team>_leader, so normal logins are not
    # stripped of permissions while the QR remains least-privilege.
    if role not in user.roles:
        user.roles.append(role)
    user.callsign = callsign
    db.session.add(user)
    db.session.commit()
    return user


@api_v1.route("/game/qr-login", methods=["POST"])
def game_qr_login():
    """Exchange a permanent physical CO QR bearer token for a same-day portal login."""
    data = request.get_json(silent=True) or {}
    team_key = str(data.get("team", "")).lower().strip()
    supplied_token = str(data.get("token", "")).strip()

    if team_key not in GAME_TEAMS:
        return jsonify({"error": "Unknown game team"}), 404

    team = GAME_TEAMS[team_key]
    expected_token = _co_qr_token(team_key)
    if not expected_token:
        return jsonify({"error": "CO QR login is not configured for this team"}), 503
    if not supplied_token or not hmac.compare_digest(supplied_token, expected_token):
        return jsonify({"error": "Invalid CO QR code"}), 401

    try:
        user = _ensure_co_qr_user(team_key)
        cutoff = _next_cutoff()
        now = datetime.now(GAME_TZ)
        session_lifetime = cutoff.astimezone(timezone.utc) - now.astimezone(timezone.utc)
        if session_lifetime.total_seconds() < 60:
            session_lifetime = timedelta(minutes=1)

        effective_roles = [team["leader_role"]]
        qr_version = _co_qr_version(team_key)
        claims = {
            "username": _co_portal_username(team_key),
            "roles": effective_roles,
            "game_qr_team": team_key,
            "game_qr_version": qr_version,
        }
        access_token = create_access_token(
            identity=str(user.id),
            additional_claims=claims,
            expires_delta=session_lifetime,
        )
        refresh_token = create_refresh_token(
            identity=str(user.id),
            additional_claims={
                "game_qr_team": team_key,
                "game_qr_version": qr_version,
            },
            expires_delta=session_lifetime,
        )

        current_app.logger.info(
            "CO QR login accepted for team %s as %s",
            team_key,
            _co_portal_username(team_key),
        )
        return jsonify({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "redirectPath": f"/{team_key}",
            "expiresAt": cutoff.isoformat(),
            "user": {
                "id": user.id,
                "username": _co_portal_username(team_key),
                "email": user.email,
                "callsign": user.callsign,
                "roles": [
                    {
                        "name": team["leader_role"],
                        "displayName": (
                            "Game Leader Red"
                            if team_key == "red"
                            else "Game Leader Blue"
                        ),
                    }
                ],
                "has_password": True,
            },
        }), 200
    except Exception as exc:
        current_app.logger.exception("CO QR login failed for team %s", team_key)
        return jsonify({"error": f"CO QR login failed: {exc}"}), 500


def _co_qr_admin_payload():
    teams = {}
    for team_key in GAME_TEAMS:
        token = _co_qr_token(team_key)
        teams[team_key] = {
            "configured": bool(token),
            "url": _co_qr_public_url(team_key, token),
            "version": _co_qr_version(team_key),
            "username": _co_portal_username(team_key),
        }
    return {"teams": teams}


@api_v1.route("/game/admin/co-qr", methods=["GET"])
@jwt_required()
def game_admin_co_qr():
    """Return printable permanent CO QR URLs to a full portal administrator."""
    error = _require_game_admin()
    if error:
        return error
    return jsonify(_co_qr_admin_payload()), 200


@api_v1.route("/game/admin/co-qr/<team_key>/reset", methods=["POST"])
@jwt_required()
def game_admin_reset_co_qr(team_key):
    """Rotate one CO QR immediately and invalidate older QR-derived sessions."""
    error = _require_game_admin()
    if error:
        return error
    if team_key not in GAME_TEAMS:
        return jsonify({"error": "Unknown game team"}), 404

    try:
        _rotate_co_qr_token(team_key)
        cleanup = _cleanup_tracked_team_sessions(team_key, "co_qr_reset")
        payload = _co_qr_admin_payload()
        payload["playerCleanup"] = cleanup
        current_app.logger.warning(
            "Administrator rotated CO QR for team %s and revoked %s temporary player session(s)",
            team_key,
            cleanup["sessionsClosed"],
        )
        return jsonify(payload), 207 if cleanup["failures"] else 200
    except Exception as exc:
        current_app.logger.exception("Failed to rotate CO QR for team %s", team_key)
        return jsonify({"error": f"CO QR reset failed: {exc}"}), 500


@api_v1.route("/game/admin/co-qr/reset-all", methods=["POST"])
@jwt_required()
def game_admin_reset_all_co_qr():
    """Rotate both CO QR codes immediately."""
    error = _require_game_admin()
    if error:
        return error

    try:
        cleanup_by_team = {}
        failures = []
        for team_key in GAME_TEAMS:
            _rotate_co_qr_token(team_key)
            cleanup = _cleanup_tracked_team_sessions(team_key, "co_qr_reset_all")
            cleanup_by_team[team_key] = cleanup
            failures.extend(cleanup["failures"])

        payload = _co_qr_admin_payload()
        payload["playerCleanup"] = cleanup_by_team
        current_app.logger.warning(
            "Administrator rotated all CO QR codes and revoked temporary player sessions"
        )
        return jsonify(payload), 207 if failures else 200
    except Exception as exc:
        current_app.logger.exception("Failed to rotate all CO QR codes")
        return jsonify({"error": f"CO QR reset failed: {exc}"}), 500


def _ensure_group_exists(group_name):
    response = otsClient.get_groups(name=group_name, page_size=100)
    matches = [
        item for item in _response_results(response)
        if item.get("name") == group_name
    ]
    if matches:
        return matches[0]

    otsClient.create_group(
        group_name,
        description=f"Airsoft game isolation group for {group_name}",
    )

    response = otsClient.get_groups(name=group_name, page_size=100)
    matches = [
        item for item in _response_results(response)
        if item.get("name") == group_name
    ]
    if not matches:
        raise RuntimeError(f"Could not create or find OTS group {group_name}")
    return matches[0]


def _safe_remove_membership(username, group_name, direction):
    try:
        otsClient.remove_user_from_group(username, group_name, direction)
    except Exception:
        # Missing membership is already the desired state.
        pass


def _group_members(group_name):
    response = otsClient.get_group_members(group_name)
    payload = response.get("response", {}) if isinstance(response, dict) else response
    if isinstance(payload, dict):
        members = payload.get("results", payload.get("members", []))
    elif isinstance(payload, list):
        members = payload
    else:
        members = []
    return members if isinstance(members, list) else []


def _configured_game_users(config_key):
    configured = current_app.config.get(config_key, [])
    if isinstance(configured, str):
        configured = [value.strip() for value in configured.split(",") if value.strip()]
    return {str(username).strip() for username in configured if str(username).strip()}


def _permanent_full_team_users(team_key):
    """Permanent users that both send to and receive from the game team."""
    config_key = (
        "GAME_PERMANENT_USERS_RED"
        if team_key == "red"
        else "GAME_PERMANENT_USERS_BLUE"
    )
    return _configured_game_users(config_key)


def _permanent_team_observers(team_key):
    """Permanent watch-only users: OUT membership only, never IN."""
    config_key = (
        "GAME_PERMANENT_OBSERVERS_RED"
        if team_key == "red"
        else "GAME_PERMANENT_OBSERVERS_BLUE"
    )
    return _configured_game_users(config_key)


def _permanent_team_users(team_key):
    """All permanent protected users, regardless of membership direction."""
    return _permanent_full_team_users(team_key) | _permanent_team_observers(team_key)


def _ensure_permanent_team_memberships(team_key):
    """Enforce IN+OUT for CO users and OUT-only for watch-only observers."""
    group_name = GAME_TEAMS[team_key]["group"]
    _ensure_group_exists(group_name)

    members = _group_members(group_name)
    memberships = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        username = member.get("username")
        direction = member.get("direction")
        if username and direction in ("IN", "OUT"):
            memberships.add((username, direction))

    observers = _permanent_team_observers(team_key)
    full_users = _permanent_full_team_users(team_key) - observers

    desired = {}
    for username in full_users:
        desired[username] = {"IN", "OUT"}
    for username in observers:
        # Observer wins if a username is accidentally present in both lists.
        desired[username] = {"OUT"}

    added = []
    removed = []
    failures = []

    for username, desired_directions in sorted(desired.items()):
        for direction in ("IN", "OUT"):
            present = (username, direction) in memberships
            should_exist = direction in desired_directions

            if present and not should_exist:
                try:
                    # Observer IN membership is security-sensitive: do not use the
                    # best-effort helper here because it intentionally hides errors.
                    otsClient.remove_user_from_group(username, group_name, direction)
                    memberships.discard((username, direction))
                    removed.append({"username": username, "direction": direction})
                except Exception as exc:
                    failures.append({
                        "username": username,
                        "direction": direction,
                        "action": "remove",
                        "error": str(exc),
                    })
                continue

            if should_exist and not present:
                try:
                    otsClient.add_user_to_group(username, group_name, direction)
                    memberships.add((username, direction))
                    added.append({"username": username, "direction": direction})
                except Exception as exc:
                    failures.append({
                        "username": username,
                        "direction": direction,
                        "action": "add",
                        "error": str(exc),
                    })

    # Re-read OTS so an observer can never silently remain IN after a failed
    # removal. This is what keeps watch-only accounts invisible to the game teams.
    final_memberships = set()
    for member in _group_members(group_name):
        if not isinstance(member, dict):
            continue
        username = member.get("username")
        direction = member.get("direction")
        if username and direction in ("IN", "OUT"):
            final_memberships.add((username, direction))

    for username in observers:
        if (username, "IN") in final_memberships:
            failures.append({
                "username": username,
                "direction": "IN",
                "action": "verify_remove",
                "error": "watch-only observer still has IN membership",
            })

    if added or removed:
        current_app.logger.info(
            "Enforced permanent game memberships for %s (added=%s removed=%s)",
            group_name,
            added,
            removed,
        )
    if failures:
        current_app.logger.warning(
            "Could not fully enforce permanent memberships for %s: %s",
            group_name,
            failures,
        )

    return {"added": added, "removed": removed, "failures": failures}


def _enforce_reserved_team_group(team_key, allowed_username=None):
    """Keep reserved groups limited to the current session and permanent allowlist."""
    group_name = GAME_TEAMS[team_key]["group"]
    allowed_usernames = _permanent_team_users(team_key)
    if allowed_username:
        allowed_usernames.add(allowed_username)

    # Configured permanent users are authoritative: make sure they are present
    # before removing any non-allowlisted members.
    _ensure_permanent_team_memberships(team_key)

    removed = []

    for member in _group_members(group_name):
        if isinstance(member, dict):
            username = member.get("username")
            direction = member.get("direction")
        else:
            username = str(member)
            direction = None

        if not username or username in allowed_usernames:
            continue

        directions = [direction] if direction in ("IN", "OUT") else ["IN", "OUT"]
        for membership_direction in directions:
            _safe_remove_membership(username, group_name, membership_direction)

        if username not in removed:
            removed.append(username)

    if removed:
        current_app.logger.warning(
            "Removed non-game users from reserved group %s: %s",
            group_name,
            ", ".join(removed),
        )

    return removed


def _ensure_team_isolation(team_key, username):
    team = GAME_TEAMS[team_key]
    group_name = team["group"]
    _ensure_group_exists(group_name)

    # Remove every membership except the team's own group.
    groups_response = otsClient.get_groups(page=1, page_size=500)
    for group in _response_results(groups_response):
        existing_group = group.get("name")
        if not existing_group or existing_group == group_name:
            continue
        for direction in ("IN", "OUT"):
            _safe_remove_membership(username, existing_group, direction)

    # Rebuild desired membership so direction state is deterministic.
    for direction in ("IN", "OUT"):
        _safe_remove_membership(username, group_name, direction)
        otsClient.add_user_to_group(username, group_name, direction)

    # Game team groups are reserved for the current temporary session plus
    # explicitly configured permanent OTS users.
    _enforce_reserved_team_group(team_key, username)


def _get_user_euds(username):
    if not username:
        return []
    response = otsClient.get_euds(page=1, page_size=500, username=username)
    return _response_results(response)


def _sync_team_color_profiles(team_key, username, euds=None):
    team = GAME_TEAMS[team_key]
    euds = euds if euds is not None else _get_user_euds(username)
    synced = 0
    errors = []

    for eud in euds:
        uid = eud.get("uid")
        if not uid:
            continue
        try:
            otsClient.create_device_profile(
                preference_key="locationTeam",
                preference_value=team["team_color"],
                value_class="class java.lang.String",
                enrollment=False,
                connection=True,
                active=True,
                eud_uid=uid,
            )
            otsClient.create_device_profile(
                preference_key="atakRoleType",
                preference_value="Team Member",
                value_class="class java.lang.String",
                enrollment=False,
                connection=True,
                active=True,
                eud_uid=uid,
            )
            synced += 1
        except Exception as exc:
            errors.append({"uid": uid, "error": str(exc)})

    return {"synced": synced, "errors": errors}


def _is_permanent_game_user(username):
    if not username:
        return False
    return any(
        username in _permanent_team_users(team_key)
        for team_key in GAME_TEAMS
    )


def _ots_user_exists(username):
    if not username:
        return False
    response = otsClient.get_users(username=username, page_size=100)
    return any(
        item.get("username") == username
        for item in _response_results(response)
        if isinstance(item, dict)
    )


def _mark_session_delete_state(team_key, username, deleted=False, reason=None):
    with _STATE_LOCK:
        state = _load_state_unlocked()
        for session in state["teams"][team_key]["sessions"]:
            if session.get("username") != username:
                continue
            if deleted:
                session["deletedAt"] = datetime.now(GAME_TZ).isoformat()
                session["deletePendingReason"] = None
            else:
                session["deletePendingReason"] = reason
        _save_state_unlocked(state)


def _try_delete_closed_session_user(team_key, session):
    """Delete a closed temporary OTS user once no EUD is still connected."""
    username = session.get("username")
    if not username:
        return {
            "username": None,
            "userDeleted": False,
            "deletionPending": False,
            "deleteReason": "missing_username",
        }

    if session.get("deletedAt"):
        return {
            "username": username,
            "userDeleted": True,
            "deletionPending": False,
            "deleteReason": None,
        }

    # Defense in depth: configured permanent game accounts are never deleted.
    if _is_permanent_game_user(username):
        _mark_session_delete_state(
            team_key,
            username,
            deleted=False,
            reason="permanent_user_protected",
        )
        return {
            "username": username,
            "userDeleted": False,
            "deletionPending": False,
            "deleteReason": "permanent_user_protected",
        }

    euds = _get_user_euds(username)
    connected = [
        eud for eud in euds
        if str(eud.get("last_status", "")).lower() == "connected"
    ]
    if connected:
        _mark_session_delete_state(
            team_key,
            username,
            deleted=False,
            reason="connected_eud",
        )
        return {
            "username": username,
            "userDeleted": False,
            "deletionPending": True,
            "deleteReason": "connected_eud",
            "connectedEuds": [eud.get("uid") for eud in connected if eud.get("uid")],
        }

    # Historical sessions may already have been removed manually.
    if not _ots_user_exists(username):
        _mark_session_delete_state(team_key, username, deleted=True)
        return {
            "username": username,
            "userDeleted": True,
            "deletionPending": False,
            "deleteReason": "already_missing",
        }

    otsClient.delete_user(username)
    _mark_session_delete_state(team_key, username, deleted=True)
    return {
        "username": username,
        "userDeleted": True,
        "deletionPending": False,
        "deleteReason": None,
    }


def _current_session(team_key):
    return _read_state()["teams"][team_key].get("current")


def _team_mission(team_key):
    """Return the current OTS mission record for a game team, if it exists."""
    mission_name = GAME_TEAMS[team_key]["mission"]
    response = otsClient.get_missions(name=mission_name, page_size=100)
    for mission in _response_results(response):
        if isinstance(mission, dict) and mission.get("name") == mission_name:
            return mission
    return None


def _mission_creator_uid(team_key):
    """Pick a stable existing EUD UID to own the server-created team mission."""
    candidates = []
    current = _current_session(team_key)
    if current and current.get("username"):
        candidates.append(current["username"])

    candidates.extend(sorted(_permanent_full_team_users(team_key)))
    candidates.extend(sorted(_permanent_team_observers(team_key)))

    # The OTS service account is a useful fallback if it also owns an EUD.
    if getattr(otsClient, "username", None):
        candidates.append(otsClient.username)

    seen = set()
    for username in candidates:
        if not username or username in seen:
            continue
        seen.add(username)
        try:
            euds = _get_user_euds(username)
        except Exception:
            continue
        for eud in euds:
            uid = eud.get("uid") if isinstance(eud, dict) else None
            if uid:
                return uid

    # Mission creation in OTS 1.7.13 requires a valid EUD UID. The mission is
    # still isolated by its GroupMission association, so any existing EUD is a
    # safe technical owner when no team-specific EUD is available.
    response = otsClient.get_euds(page=1, page_size=500)
    for eud in _response_results(response):
        uid = eud.get("uid") if isinstance(eud, dict) else None
        if uid:
            return uid

    raise RuntimeError(
        "Cannot create game mission because OpenTAKServer has no EUD records yet"
    )


def _create_team_mission(team_key, creator_uid=None):
    team = GAME_TEAMS[team_key]
    group = _ensure_group_exists(team["group"])
    group_id = group.get("id") if isinstance(group, dict) else None
    if group_id is None:
        raise RuntimeError(f"OTS group {team['group']} has no group ID")

    creator_uid = creator_uid or _mission_creator_uid(team_key)
    otsClient.create_mission(
        name=team["mission"],
        creator_uid=creator_uid,
        group_ids=[int(group_id)],
        description=team["mission_description"],
        tool="public",
        default_role="MISSION_SUBSCRIBER",
    )

    mission = _team_mission(team_key)
    if not mission:
        raise RuntimeError(f"OTS mission {team['mission']} was not created")

    current_app.logger.info(
        "Created airsoft mission %s for group %s using creator UID %s",
        team["mission"],
        team["group"],
        creator_uid,
    )
    return mission


def _ensure_team_mission(team_key):
    mission = _team_mission(team_key)
    if mission:
        return {"created": False, "mission": mission}
    return {"created": True, "mission": _create_team_mission(team_key)}


def _reset_team_mission(team_key):
    """Delete the old team mission and create a fresh mission with a new GUID."""
    team = GAME_TEAMS[team_key]
    old_mission = _team_mission(team_key)
    old_guid = old_mission.get("guid") if old_mission else None

    if old_mission:
        otsClient.delete_mission(team["mission"])

    new_mission = _create_team_mission(team_key)
    return {
        "name": team["mission"],
        "oldGuid": old_guid,
        "newGuid": new_mission.get("guid"),
        "deleted": bool(old_mission),
        "created": True,
    }


def _mark_session_closed(team_key, username, reason):
    with _STATE_LOCK:
        state = _load_state_unlocked()
        team_state = state["teams"][team_key]
        for session in team_state["sessions"]:
            if session.get("username") == username:
                session["closedAt"] = datetime.now(GAME_TZ).isoformat()
                session["closeReason"] = reason
        if team_state.get("current") and team_state["current"].get("username") == username:
            team_state["current"] = None
        _save_state_unlocked(state)


def _record_session(team_key, session):
    with _STATE_LOCK:
        state = _load_state_unlocked()
        state["teams"][team_key]["sessions"].append(session)
        state["teams"][team_key]["current"] = session
        _save_state_unlocked(state)


def _revoke_session_credentials(team_key, session, reason):
    username = session.get("username")
    if not username:
        return {"username": None, "credentialsRevoked": False}

    euds = _get_user_euds(username)

    # The state file can still reference a session whose OTS user was already
    # deleted by a previous cleanup or manually. Treat that as successfully
    # closed instead of blocking generation of the next game session.
    if not _ots_user_exists(username):
        for direction in ("IN", "OUT"):
            _safe_remove_membership(username, GAME_TEAMS[team_key]["group"], direction)
        _mark_session_closed(team_key, username, reason)
        _mark_session_delete_state(team_key, username, deleted=True)
        return {
            "username": username,
            "eudCount": len(euds),
            "euds": euds,
            "credentialsRevoked": True,
            "accountDeactivated": True,
            "userDeleted": True,
            "deletionPending": False,
            "deleteReason": "already_missing",
        }

    try:
        otsClient.delete_atak_qr_string(username)
    except Exception:
        pass

    try:
        # Rotate first as defense in depth; then deactivate permanently.
        otsClient.reset_user_password(username, _random_password())
    finally:
        otsClient.deactivate_user(username)

    # Closed session accounts no longer need any team routing membership.
    for direction in ("IN", "OUT"):
        _safe_remove_membership(username, GAME_TEAMS[team_key]["group"], direction)

    _mark_session_closed(team_key, username, reason)

    # OTS 1.7.13 has a supported administrator-only /api/user/delete endpoint.
    # Do not delete while an EUD is still connected: an already-authenticated
    # TLS socket can outlive the account record. Deletion is retried by the
    # scheduled cleanup once every associated EUD is disconnected.
    deletion = _try_delete_closed_session_user(
        team_key,
        {
            "username": username,
            "deletedAt": None,
        },
    )

    return {
        "username": username,
        "eudCount": len(euds),
        "euds": euds,
        "credentialsRevoked": True,
        "accountDeactivated": True,
        **deletion,
    }


def _close_current_session(team_key, reason):
    session = _current_session(team_key)
    if not session:
        return None
    return _revoke_session_credentials(team_key, session, reason)


def _team_status(team_key):
    team = GAME_TEAMS[team_key]
    current = _current_session(team_key)
    username = current.get("username") if current else None
    outsiders_removed = _enforce_reserved_team_group(team_key, username)
    euds = _get_user_euds(username)
    profile_sync = (
        _sync_team_color_profiles(team_key, username, euds)
        if username else {"synced": 0, "errors": []}
    )

    return {
        "team": team_key,
        "accountLabel": team["label"],
        "username": username,
        "group": team["group"],
        "teamColor": team["team_color"],
        "missionName": team["mission"],
        "eudCount": len(euds),
        "euds": euds,
        "profileSync": profile_sync,
        "outsidersRemoved": outsiders_removed,
        "permanentUsers": sorted(_permanent_team_users(team_key)),
        "expiresAt": current.get("expiresAt") if current else None,
        "activeSession": bool(current),
        "hardEudDeleteAvailable": False,
    }


@api_v1.route("/game/status", methods=["GET"])
@jwt_required()
def game_status():
    teams = []

    for team_key in GAME_TEAMS:
        if _can_access_team(team_key):
            try:
                teams.append(_team_status(team_key))
            except Exception as exc:
                current_app.logger.exception("Failed to load game team status")
                teams.append({
                    "team": team_key,
                    "accountLabel": GAME_TEAMS[team_key]["label"],
                    "group": GAME_TEAMS[team_key]["group"],
                    "teamColor": GAME_TEAMS[team_key]["team_color"],
                    "error": str(exc),
                })

    if not teams:
        return jsonify({"error": "Game leader or administrator role required"}), 403

    return jsonify({
        "timezone": "Europe/Amsterdam",
        "teams": teams,
        "hardEudDeleteAvailable": False,
        "hardEudDeleteNote": (
            "Historic EUD rows remain in OpenTAKServer because it has no supported "
            "DELETE /api/eud endpoint. Security does not depend on deleting those rows: "
            "every game session has a unique OTS username that is never reactivated."
        ),
    }), 200


@api_v1.route("/game/<team_key>/access", methods=["POST"])
@jwt_required()
def generate_game_access(team_key):
    error = _require_team_access(team_key)
    if error:
        return error

    team = GAME_TEAMS[team_key]
    data = request.get_json(silent=True) or {}
    max_uses = data.get("maxUses", current_app.config.get("GAME_QR_MAX_USES", 250))

    try:
        max_uses = max(1, min(int(max_uses), 5000))
    except (TypeError, ValueError):
        return jsonify({"error": "maxUses must be a positive integer"}), 400

    try:
        # A refresh is a new security generation: permanently close the old account.
        previous = _close_current_session(team_key, "superseded")
        if previous:
            current_app.logger.info(
                "Closed previous %s game session %s", team_key, previous["username"]
            )

        password = _human_game_password()
        username = None
        last_error = None

        # Human-friendly four-digit suffixes can collide, so retry generously.
        for _ in range(10):
            candidate = _new_session_username(team_key)
            try:
                otsClient.create_user(candidate, password, roles=["user"])
                username = candidate
                break
            except Exception as exc:
                last_error = exc

        if not username:
            raise RuntimeError(f"Could not create unique game user: {last_error}")

        _ensure_team_isolation(team_key, username)
        otsClient.activate_user(username)

        cutoff = _next_cutoff()
        expires_at = int(cutoff.timestamp())

        atak_response = otsClient.create_atak_qr_string(
            username=username,
            exp=expires_at,
            max_uses=max_uses,
        )
        atak_payload = _response_payload(atak_response)

        itak_response = otsClient.get_itak_qr_string(username)
        itak_payload = (
            itak_response.get("response")
            if isinstance(itak_response, dict)
            else itak_response
        )
        if isinstance(itak_payload, dict):
            itak_string = itak_payload.get("qr_string") or itak_payload.get("response")
        else:
            itak_string = itak_payload

        session = {
            "username": username,
            "createdAt": datetime.now(GAME_TZ).isoformat(),
            "expiresAt": cutoff.isoformat(),
            "closedAt": None,
            "closeReason": None,
            "deletedAt": None,
            "deletePendingReason": None,
        }
        _record_session(team_key, session)

        return jsonify({
            "team": team_key,
            "accountLabel": team["label"],
            "username": username,
            "password": password,
            "group": team["group"],
            "teamColor": team["team_color"],
            "atak": {
                "qrString": atak_payload.get("qr_string"),
                "expiresAt": atak_payload.get("exp", expires_at),
                "maxUses": atak_payload.get("max", max_uses),
            },
            "itak": {
                "qrString": itak_string,
                "expiresAt": expires_at,
            },
            "expiresAt": cutoff.isoformat(),
            "passwordShownOnce": True,
        }), 200

    except Exception as exc:
        current_app.logger.exception("Failed to generate game access for %s", team_key)

        # A request can fail after OTS already created the temporary account.
        # If that happens before _record_session(), immediately make the partial
        # account unusable and remove its team memberships.
        partial_username = locals().get("username")
        if partial_username:
            try:
                otsClient.deactivate_user(partial_username)
            except Exception:
                current_app.logger.exception(
                    "Failed to deactivate partial game account %s", partial_username
                )
            for direction in ("IN", "OUT"):
                _safe_remove_membership(
                    partial_username,
                    GAME_TEAMS[team_key]["group"],
                    direction,
                )

        return jsonify({"error": f"Failed to generate game access: {exc}"}), 500


def _cleanup_tracked_team_sessions(team_key, reason):
    """Revoke every still-open temporary game session for one team."""
    state = _read_state()
    sessions = list(state["teams"][team_key].get("sessions", []))
    results = []
    failures = []

    for session in sessions:
        if session.get("closedAt"):
            continue
        try:
            results.append(_revoke_session_credentials(team_key, session, reason))
        except Exception as exc:
            failures.append({
                "team": team_key,
                "username": session.get("username"),
                "error": str(exc),
            })

    return {
        "team": team_key,
        "sessionsClosed": len(results),
        "eudCount": sum(item.get("eudCount", 0) for item in results),
        "results": results,
        "failures": failures,
    }


@api_v1.route("/game/admin/cleanup/<team_key>", methods=["POST"])
@jwt_required()
def cleanup_game_team(team_key):
    error = _require_game_admin()
    if error:
        return error
    if team_key not in GAME_TEAMS:
        return jsonify({"error": "Unknown game team"}), 404

    cleanup = _cleanup_tracked_team_sessions(team_key, "admin_cleanup")
    cleanup["hardEudDeleted"] = False
    cleanup["note"] = (
        "Closed temporary OTS users are deleted once none of their EUDs is still connected. "
        "Historic OTS EUD rows remain because OTS exposes no supported EUD DELETE API."
    )
    return jsonify(cleanup), 207 if cleanup["failures"] else 200


@api_v1.route("/game/admin/cleanup", methods=["POST"])
@jwt_required()
def cleanup_all_game_teams():
    error = _require_game_admin()
    if error:
        return error

    results = []
    failures = []
    teams = {}
    for team_key in GAME_TEAMS:
        cleanup = _cleanup_tracked_team_sessions(team_key, "admin_cleanup_all")
        teams[team_key] = cleanup
        for result in cleanup["results"]:
            result["team"] = team_key
            results.append(result)
        failures.extend(cleanup["failures"])

    return jsonify({
        "teams": teams,
        "results": results,
        "failures": failures,
    }), 207 if failures else 200


@scheduler.task(
    id="airsoft_game_membership_guard",
    trigger="cron",
    minute="*/5",
    timezone="Europe/Amsterdam",
    misfire_grace_time=300,
    max_instances=1,
)
def airsoft_game_membership_guard():
    """Continuously enforce the reserved GameRed/GameBlue membership policy."""
    with scheduler.app.app_context():
        for team_key in GAME_TEAMS:
            try:
                current = _current_session(team_key)
                current_username = current.get("username") if current else None
                removed = _enforce_reserved_team_group(team_key, current_username)
                if removed:
                    current_app.logger.warning(
                        "Airsoft membership guard removed unexpected users from %s: %s",
                        GAME_TEAMS[team_key]["group"],
                        ", ".join(removed),
                    )
            except Exception:
                current_app.logger.exception(
                    "Airsoft membership guard failed for %s",
                    team_key,
                )


@scheduler.task(
    id="airsoft_game_mission_guard",
    trigger="cron",
    minute="2-59/5",
    timezone="Europe/Amsterdam",
    misfire_grace_time=300,
    max_instances=1,
)
def airsoft_game_mission_guard():
    """Create a missing GameRed/GameBlue mission without waiting for 23:00."""
    with scheduler.app.app_context():
        for team_key in GAME_TEAMS:
            try:
                result = _ensure_team_mission(team_key)
                if result["created"]:
                    current_app.logger.info(
                        "Airsoft mission guard created missing mission %s",
                        GAME_TEAMS[team_key]["mission"],
                    )
            except Exception:
                current_app.logger.exception(
                    "Airsoft mission guard failed for %s",
                    team_key,
                )


@scheduler.task(
    id="airsoft_game_user_delete_retry",
    trigger="cron",
    minute="*/5",
    timezone="Europe/Amsterdam",
    misfire_grace_time=300,
    max_instances=1,
)
def airsoft_game_user_delete_retry():
    """Delete closed temporary OTS users after their EUDs disconnect."""
    with scheduler.app.app_context():
        state = _read_state()
        for team_key in GAME_TEAMS:
            for session in state["teams"][team_key].get("sessions", []):
                if not session.get("closedAt") or session.get("deletedAt"):
                    continue
                try:
                    result = _try_delete_closed_session_user(team_key, session)
                    if result.get("userDeleted"):
                        current_app.logger.info(
                            "Deleted closed airsoft game user %s",
                            session.get("username"),
                        )
                except Exception:
                    current_app.logger.exception(
                        "Deferred deletion failed for airsoft game user %s",
                        session.get("username"),
                    )


@scheduler.task(
    id="airsoft_game_credential_cutoff",
    trigger="cron",
    hour=23,
    minute=0,
    timezone="Europe/Amsterdam",
    misfire_grace_time=3600,
    max_instances=1,
)
def airsoft_game_credential_cutoff():
    """Deactivate team sessions and replace both team missions at 23:00."""
    with scheduler.app.app_context():
        for team_key in GAME_TEAMS:
            try:
                result = _close_current_session(team_key, "23:00_cutoff")
                if result:
                    current_app.logger.info(
                        "23:00 airsoft cutoff completed for %s (%s, %s EUDs)",
                        team_key,
                        result["username"],
                        result["eudCount"],
                    )
            except Exception:
                current_app.logger.exception(
                    "23:00 airsoft cutoff failed for %s", team_key
                )

        for team_key in GAME_TEAMS:
            try:
                mission_result = _reset_team_mission(team_key)
                current_app.logger.info(
                    "23:00 mission reset completed for %s (%s -> %s)",
                    team_key,
                    mission_result["oldGuid"],
                    mission_result["newGuid"],
                )
            except Exception:
                current_app.logger.exception(
                    "23:00 mission reset failed for %s", team_key
                )
