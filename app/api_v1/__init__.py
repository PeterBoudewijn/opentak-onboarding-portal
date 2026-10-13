"""
API v1 Blueprint
RESTful API endpoints for the OpenTAK Onboarding Portal
"""

from flask import Blueprint

# Create the API v1 blueprint
api_v1 = Blueprint('api_v1', __name__, url_prefix='/api/v1')


# Physical CO QR sessions are bearer credentials. Keep them constrained to the
# airsoft workflow even though they are represented by normal portal JWTs.
# This prevents a stolen/photographed CO QR from being used against unrelated
# authenticated endpoints such as password changes, generic enrollment QR
# generation, user/profile management, etc.
@api_v1.before_request
def restrict_game_qr_session_scope():
    from flask import jsonify, request
    from flask_jwt_extended import get_jwt, verify_jwt_in_request

    if request.method == "OPTIONS":
        return None

    try:
        verify_jwt_in_request(optional=True)
        claims = get_jwt()
    except Exception:
        # Public endpoints and endpoints with their own JWT requirements keep
        # their existing behavior.
        return None

    team_key = claims.get("game_qr_team")
    if team_key not in ("red", "blue"):
        return None

    allowed_paths = {
        "/api/v1/auth/me",
        "/api/v1/auth/permissions",
        "/api/v1/auth/refresh",
        "/api/v1/game/status",
        f"/api/v1/game/{team_key}/access",
        f"/api/v1/game/{team_key}/co-tak-access",
    }

    if request.path in allowed_paths:
        return None

    return jsonify({
        "error": "CO QR sessions are restricted to the airsoft game portal",
        "code": "GAME_QR_SCOPE_RESTRICTED",
    }), 403

# Import all route modules to register them
from app.api_v1 import auth, users, roles, onboarding_codes, tak_profiles, meshtastic, meshtastic_groups, radios, settings, qr, pending_registrations, version, announcements, api_keys, approvals, logo, oidc, kiosk, magic_link, groups, game
