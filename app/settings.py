from os import environ
from dotenv import load_dotenv
from urllib.parse import urlparse
import secrets


def _env_bool(name, default='False'):
    return str(environ.get(name, default)).strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_csv(name, default=''):
    return [
        value.strip()
        for value in str(environ.get(name, default)).split(',')
        if value.strip()
    ]


load_dotenv()
API_KEY = str(environ.get('API_KEY', secrets.token_hex(32)))
SECRET_KEY=str(environ.get('SECRET_KEY'))
OTS_USERNAME=str(environ.get('OTS_USERNAME'))
OTS_PASSWORD=str(environ.get('OTS_PASSWORD'))
OTS_URL=str(environ.get('OTS_URL'))
OTS_HOSTNAME=urlparse(OTS_URL).hostname
OTS_VERIFY_SSL=_env_bool('OTS_VERIFY_SSL', 'True')
DEBUG=_env_bool('DEBUG', 'False')
SQLALCHEMY_DATABASE_URI=str(environ.get('SQLALCHEMY_DATABASE_URI', 'sqlite:///db.sqlite'))
MAIL_SERVER=str(environ.get('MAIL_SERVER'))
MAIL_PORT=int(environ.get('MAIL_PORT'))
MAIL_USE_TLS=_env_bool('MAIL_USE_TLS', 'False')
MAIL_USE_SSL=_env_bool('MAIL_USE_SSL', 'False')
MAIL_USERNAME=str(environ.get('MAIL_USERNAME'))
MAIL_PASSWORD=str(environ.get('MAIL_PASSWORD'))
MAIL_DEFAULT_SENDER=str(environ.get('MAIL_DEFAULT_SENDER'))
MAIL_ENABLED=_env_bool('MAIL_ENABLED', 'False')
DATAPACKAGE_UPLOAD_FOLDER = (environ.get('DATAPACKAGE_UPLOAD_FOLDER','datapackages'))
UPDATES_UPLOAD_FOLDER = str(environ.get('UPDATES_UPLOAD_FOLDER', 'updates'))

JWT_SECRET_KEY = str(environ.get('JWT_SECRET_KEY', secrets.token_hex(32)))
# Allow JWT identity to be any JSON-serializable type (not just string)
JWT_IDENTITY_CLAIM = 'sub'
JWT_ALGORITHM = 'HS256'

TESTING = _env_bool('TESTING', 'False')

# Frontend URL for password reset links
FRONTEND_URL = str(environ.get('FRONTEND_URL', 'http://localhost:5000'))

# CORS origins for API
CORS_ORIGINS = str(environ.get('CORS_ORIGINS', '*'))

# OTS usernames that may remain permanently in the reserved airsoft groups.
# Use comma-separated usernames when more than one permanent account is needed.
GAME_PERMANENT_USERS_RED = _env_csv('GAME_PERMANENT_USERS_RED')
GAME_PERMANENT_USERS_BLUE = _env_csv('GAME_PERMANENT_USERS_BLUE')

# Permanent watch-only users. They receive team traffic through OUT membership
# but are deliberately kept out of IN so their own PLI is not routed to the team.
GAME_PERMANENT_OBSERVERS_RED = _env_csv('GAME_PERMANENT_OBSERVERS_RED')
GAME_PERMANENT_OBSERVERS_BLUE = _env_csv('GAME_PERMANENT_OBSERVERS_BLUE')


# Permanent bearer tokens used by the physical CO portal QR codes.
# Generate these with scripts/installers/setup_game_co_qr_tokens.py.
GAME_CO_QR_TOKEN_RED = str(environ.get('GAME_CO_QR_TOKEN_RED', '')).strip()
GAME_CO_QR_TOKEN_BLUE = str(environ.get('GAME_CO_QR_TOKEN_BLUE', '')).strip()
