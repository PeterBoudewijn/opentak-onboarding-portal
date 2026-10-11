"""
Airsoft game mode API.

Provides isolated GameRed/GameBlue access on top of OpenTAKServer without
modifying OTS database tables directly.

Security model:
- GameRed belongs only to the GameRed OTS group (IN + OUT)
- GameBlue belongs only to the GameBlue OTS group (IN + OUT)
- red/blue game leaders only see their own team
- only the full portal administrator can close/clean up teams
- ATAK enrollment tokens expire at the next 23:00 Europe/Amsterdam,
  capped at 24 hours
- at 23:00 both game accounts are deactivated, passwords are rotated and
  ATAK QR tokens are invalidated. EUD row deletion is deliberately not done
  here because OpenTAKServer does not expose a supported DELETE /api/eud API.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import secrets
import string

from flask import current_app, jsonify, request
from flask_jwt_extended import jwt_required

from app.api_v1 import api_v1
from app.extensions import scheduler
from app.ots import otsClient
from app.rbac import get_user_roles


GAME_TZ = ZoneInfo("Europe/Amsterdam")
GAME_TEAMS = {
    "red": {
        "username": "GameRed",
        "group": "GameRed",
        "team_color": "Red",
        "leader_role": "game_red_leader",
    },
    "blue": {
        "username": "GameBlue",
        "group": "GameBlue",
        "team_color": "Blue",
        "leader_role": "game_blue_leader",
    },
}


def _random_password(length=24):
    alphabet = string.ascii_letters + string.digits + "!@#$%*-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _next_cutoff(now=None):
    """Return the earliest of next local 23:00 or exactly 24 hours from now."""
    now = now or datetime.now(GAME_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=GAME_TZ)
    else:
        now = now.astimezone(GAME_TZ)

    cutoff = now.replace(hour=23, minute=0, second=0, microsecond=0)
    if now >= cutoff:
        cutoff += timedelta(days=1)

    # Compute the 24-hour cap on the UTC timeline. Local-time arithmetic can
    # otherwise last 25 real hours across the autumn DST transition.
    hard_limit = (now.astimezone(timezone.utc) + timedelta(hours=24)).astimezone(GAME_TZ)
    return cutoff if cutoff.timestamp() <= hard_limit.timestamp() else hard_limit


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
    return bool(team and team["leader_role"] in roles)


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


def _ensure_group_exists(group_name):
    response = otsClient.get_groups(name=group_name, page_size=100)
    matches = [
        item for item in _response_results(response)
        if item.get("name") == group_name
    ]
    if not matches:
        otsClient.create_group(
            group_name,
            description=f"Airsoft game isolation group for {group_name}",
        )


def _safe_remove_membership(username, group_name, direction):
    try:
        otsClient.remove_user_from_group(username, group_name, direction)
    except Exception:
        # Missing membership is already the desired state.
        pass


def _ensure_team_isolation(team_key):
    team = GAME_TEAMS[team_key]
    username = team["username"]
    group_name = team["group"]

    _ensure_group_exists(group_name)

    # Remove every membership except the team's own group. This prevents
    # accidental visibility through __ANON__ or an unrelated OTS group.
    groups_response = otsClient.get_groups(page=1, page_size=500)
    for group in _response_results(groups_response):
        existing_group = group.get("name")
        if not existing_group or existing_group == group_name:
            continue
        for direction in ("IN", "OUT"):
            _safe_remove_membership(username, existing_group, direction)

    # Rebuild the desired memberships so stale direction state cannot survive.
    for direction in ("IN", "OUT"):
        _safe_remove_membership(username, group_name, direction)
        otsClient.add_user_to_group(username, group_name, direction)


def _get_team_euds(team_key):
    username = GAME_TEAMS[team_key]["username"]
    response = otsClient.get_euds(page=1, page_size=500, username=username)
    return _response_results(response)


def _sync_team_color_profiles(team_key, euds=None):
    """
    Configure ATAK connection profiles for discovered EUDs.

    OTS supports the ATAK preference key locationTeam. Profiles are attached
    per EUD UID so GameRed and GameBlue can use different colors on one server.
    """
    team = GAME_TEAMS[team_key]
    euds = euds if euds is not None else _get_team_euds(team_key)
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


def _team_status(team_key):
    team = GAME_TEAMS[team_key]
    euds = _get_team_euds(team_key)
    profile_sync = _sync_team_color_profiles(team_key, euds)

    return {
        "team": team_key,
        "username": team["username"],
        "group": team["group"],
        "teamColor": team["team_color"],
        "eudCount": len(euds),
        "euds": euds,
        "profileSync": profile_sync,
        "nextCutoff": _next_cutoff().isoformat(),
        "hardEudDeleteAvailable": False,
    }


@api_v1.route("/game/status", methods=["GET"])
@jwt_required()
def game_status():
    roles = set(get_user_roles())
    teams = []

    for team_key in GAME_TEAMS:
        if "administrator" in roles or GAME_TEAMS[team_key]["leader_role"] in roles:
            try:
                teams.append(_team_status(team_key))
            except Exception as exc:
                current_app.logger.exception("Failed to load game team status")
                teams.append({
                    "team": team_key,
                    "username": GAME_TEAMS[team_key]["username"],
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
            "OpenTAKServer has no supported DELETE /api/eud endpoint. "
            "The portal can invalidate credentials and deactivate game accounts; "
            "physical EUD-row deletion requires an OTS-side helper."
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
        _ensure_team_isolation(team_key)
        otsClient.activate_user(team["username"])

        password = _random_password()
        otsClient.reset_user_password(team["username"], password)

        cutoff = _next_cutoff()
        expires_at = int(cutoff.timestamp())

        try:
            otsClient.delete_atak_qr_string(team["username"])
        except Exception:
            pass

        atak_response = otsClient.create_atak_qr_string(
            username=team["username"],
            exp=expires_at,
            max_uses=max_uses,
        )
        atak_payload = _response_payload(atak_response)

        itak_response = otsClient.get_itak_qr_string(team["username"])
        itak_payload = itak_response.get("response") if isinstance(itak_response, dict) else itak_response
        if isinstance(itak_payload, dict):
            itak_string = itak_payload.get("qr_string") or itak_payload.get("response")
        else:
            itak_string = itak_payload

        euds = _get_team_euds(team_key)
        profile_sync = _sync_team_color_profiles(team_key, euds)

        return jsonify({
            "team": team_key,
            "username": team["username"],
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
            "profileSync": profile_sync,
            "passwordShownOnce": True,
        }), 200

    except Exception as exc:
        current_app.logger.exception("Failed to generate game access for %s", team_key)
        return jsonify({"error": f"Failed to generate game access: {exc}"}), 500


def _close_team(team_key):
    team = GAME_TEAMS[team_key]
    euds = _get_team_euds(team_key)

    try:
        otsClient.delete_atak_qr_string(team["username"])
    except Exception:
        pass

    # Rotate before deactivating so a later accidental reactivation cannot expose
    # yesterday's password.
    otsClient.reset_user_password(team["username"], _random_password())
    otsClient.deactivate_user(team["username"])

    return {
        "team": team_key,
        "username": team["username"],
        "eudCount": len(euds),
        "euds": euds,
        "credentialsRevoked": True,
        "accountDeactivated": True,
        "hardEudDeleted": False,
        "note": (
            "Credentials were revoked and the shared game account was deactivated. "
            "OpenTAKServer does not expose a supported EUD DELETE API, so historic "
            "EUD rows are not physically deleted by this endpoint."
        ),
    }


@api_v1.route("/game/admin/cleanup/<team_key>", methods=["POST"])
@jwt_required()
def cleanup_game_team(team_key):
    error = _require_game_admin()
    if error:
        return error
    if team_key not in GAME_TEAMS:
        return jsonify({"error": "Unknown game team"}), 404

    try:
        result = _close_team(team_key)
        current_app.logger.warning(
            "Airsoft cleanup requested for %s: %s EUD(s)",
            team_key,
            result["eudCount"],
        )
        return jsonify(result), 200
    except Exception as exc:
        current_app.logger.exception("Airsoft cleanup failed for %s", team_key)
        return jsonify({"error": f"Cleanup failed: {exc}"}), 500


@api_v1.route("/game/admin/cleanup", methods=["POST"])
@jwt_required()
def cleanup_all_game_teams():
    error = _require_game_admin()
    if error:
        return error

    results = []
    failures = []
    for team_key in GAME_TEAMS:
        try:
            results.append(_close_team(team_key))
        except Exception as exc:
            failures.append({"team": team_key, "error": str(exc)})

    status = 207 if failures else 200
    return jsonify({"results": results, "failures": failures}), status


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
    """Automatically revoke game credentials at 23:00 local time."""
    with scheduler.app.app_context():
        for team_key in GAME_TEAMS:
            try:
                result = _close_team(team_key)
                current_app.logger.info(
                    "23:00 airsoft cutoff completed for %s (%s EUDs recorded)",
                    team_key,
                    result["eudCount"],
                )
            except Exception:
                current_app.logger.exception(
                    "23:00 airsoft cutoff failed for %s", team_key
                )
