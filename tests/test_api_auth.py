"""
Tests for authentication API endpoints
"""
import pytest
import requests_mock


class TestAuthLogin:
    """Test /auth/login endpoint"""

    def test_login_success(self, client, db):
        """Test successful login"""
        # Create test user in database
        from app.models import UserModel, UserRoleModel

        # Create user role
        role = UserRoleModel.query.filter_by(name='user').first()
        if not role:
            role = UserRoleModel.create_role(name='user', description='User role')

        # Create user
        user = UserModel.query.filter_by(username='testuser').first()
        if not user:
            user = UserModel.create_user(
                username='testuser',
                email='test@example.com',
                firstname='Test',
                lastname='User',
                callsign='TEST'
            )
            user.roles.append(role)
            db.session.commit()

        with requests_mock.Mocker() as m:
            # Mock OTS login response
            m.post('http://localhost:8080/api/login', json={
                'token': 'mock-token',
                'user': {
                    'username': 'testuser',
                    'email': 'test@example.com',
                    'roles': ['user']
                }
            })

            # Mock OTS get_me response
            m.get('http://localhost:8080/api/me', json={
                'username': 'testuser',
                'email': 'test@example.com',
                'callsign': 'TEST',
                'firstName': 'Test',
                'lastName': 'User',
                'roles': ['user']
            })

            response = client.post('/api/v1/auth/login', json={
                'username': 'testuser',
                'password': 'password'
            })

            # Debug output
            if response.status_code != 200:
                print(f"Login failed with status {response.status_code}")
                print(f"Response: {response.get_json()}")

            assert response.status_code == 200
            data = response.get_json()
            assert 'access_token' in data
            assert 'refresh_token' in data
            assert 'user' in data

    def test_login_missing_credentials(self, client):
        """Test login with missing credentials"""
        response = client.post('/api/v1/auth/login', json={
            'username': 'testuser'
        })

        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data

    def test_login_invalid_credentials(self, client):
        """Test login with invalid credentials"""
        with requests_mock.Mocker() as m:
            # Mock OTS 401 response
            m.post('http://localhost:8080/api/login', status_code=401)

            response = client.post('/api/v1/auth/login', json={
                'username': 'baduser',
                'password': 'badpass'
            })

            assert response.status_code == 401
            data = response.get_json()
            assert 'error' in data


class TestAuthMe:
    """Test /auth/me endpoint"""

    def test_get_current_user_success(self, client, auth_headers):
        """Test getting current user with valid token"""
        print(f"Auth headers: {auth_headers}")
        response = client.get('/api/v1/auth/me', headers=auth_headers)

        if response.status_code != 200:
            print(f"Failed with status {response.status_code}")
            print(f"Response: {response.get_json()}")

        assert response.status_code == 200
        data = response.get_json()
        assert 'username' in data
        assert 'email' in data

    def test_get_current_user_no_auth(self, client):
        """Test getting current user without authentication"""
        response = client.get('/api/v1/auth/me')

        assert response.status_code == 401


class TestAuthRegister:
    """Test /auth/register endpoint"""

    def test_register_with_invalid_code(self, client):
        """Test registration with invalid onboarding code"""
        response = client.post('/api/v1/auth/register', json={
            'username': 'newuser',
            'password': 'password',
            'email': 'new@example.com',
            'firstName': 'New',
            'lastName': 'User',
            'callsign': 'NEW',
            'onboardingCode': 'INVALID'
        })

        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data

    def test_register_missing_fields(self, client):
        """Test registration with missing required fields"""
        response = client.post('/api/v1/auth/register', json={
            'username': 'newuser',
            'password': 'password'
        })

        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data


class TestPendingRegistrationPasswordEncryption:
    """Test encryption of temporary pending-registration passwords."""

    def test_password_is_encrypted_at_rest_and_decrypts_for_ots(self, app):
        from datetime import datetime, timedelta
        from app.models import PendingRegistrationModel, PENDING_PASSWORD_PREFIX

        plaintext = 'TemporaryPassword-123!'

        with app.app_context():
            pending = PendingRegistrationModel(
                username='pendingcrypto',
                email='pendingcrypto@example.com',
                password=plaintext,
                firstName='Pending',
                lastName='Crypto',
                callsign='CRYPT',
                onboarding_code_id=1,
                verification_token='pending-crypto-token',
                expires_at=datetime.now() + timedelta(hours=1),
            )

            assert pending._password.startswith(PENDING_PASSWORD_PREFIX)
            assert plaintext not in pending._password
            assert pending.password == plaintext

    def test_unencrypted_legacy_value_is_rejected(self, app):
        from app.models import PendingRegistrationModel

        with app.app_context():
            pending = PendingRegistrationModel()
            pending._password = 'legacy-plaintext-password'

            with pytest.raises(RuntimeError, match='not encrypted'):
                _ = pending.password


class TestOIDCExchangeSecurity:
    """Test the one-time OIDC-to-JWT browser handoff."""

    def test_oidc_exchange_code_is_hashed_and_single_use(self, client, sample_user):
        import hashlib
        from datetime import datetime, timedelta
        from app.models import OneTimeTokenModel

        code = 'test-oidc-one-time-code'
        code_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()

        token = OneTimeTokenModel.create_token(
            user_id=sample_user.id,
            token=code_hash,
            token_type='oidc_exchange',
            expires_at=datetime.now() + timedelta(minutes=2),
        )
        assert token.token == code_hash
        assert token.token != code

        with client.session_transaction() as browser_session:
            browser_session['oidc_exchange_hash'] = code_hash

        response = client.post('/api/v1/auth/oidc/exchange', json={'code': code})
        assert response.status_code == 200
        data = response.get_json()
        assert 'access_token' in data
        assert 'refresh_token' in data

        # Even if the browser session hash were replayed, the DB token is spent.
        with client.session_transaction() as browser_session:
            browser_session['oidc_exchange_hash'] = code_hash

        replay = client.post('/api/v1/auth/oidc/exchange', json={'code': code})
        assert replay.status_code == 400
        assert replay.get_json()['code'] == 'INVALID_OIDC_EXCHANGE'

    def test_oidc_exchange_code_is_bound_to_browser_session(self, client, sample_user):
        import hashlib
        from datetime import datetime, timedelta
        from app.models import OneTimeTokenModel

        code = 'test-oidc-browser-bound-code'
        code_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()

        OneTimeTokenModel.create_token(
            user_id=sample_user.id,
            token=code_hash,
            token_type='oidc_exchange',
            expires_at=datetime.now() + timedelta(minutes=2),
        )

        response = client.post('/api/v1/auth/oidc/exchange', json={'code': code})
        assert response.status_code == 400
        assert response.get_json()['code'] == 'INVALID_OIDC_EXCHANGE'


class TestOIDCClientSecretEncryption:
    """Test encryption of OIDC client secrets at rest."""

    def test_client_secret_is_encrypted_at_rest_and_decrypts_for_oidc(self, app):
        from app.models import OIDCProviderModel, OIDC_CLIENT_SECRET_PREFIX

        plaintext = 'oidc-test-client-secret'

        with app.app_context():
            provider = OIDCProviderModel(
                name='test-oidc',
                display_name='Test OIDC',
                discovery_url='https://idp.example/.well-known/openid-configuration',
                client_id='test-client',
                client_secret=plaintext,
            )

            assert provider._client_secret.startswith(OIDC_CLIENT_SECRET_PREFIX)
            assert plaintext not in provider._client_secret
            assert provider.client_secret == plaintext
            assert provider.to_dict(include_secrets=False)['client_secret'] == '••••••••'

    def test_unencrypted_legacy_client_secret_is_rejected(self, app):
        from app.models import OIDCProviderModel

        with app.app_context():
            provider = OIDCProviderModel()
            provider._client_secret = 'legacy-plaintext-client-secret'

            with pytest.raises(RuntimeError, match='not encrypted'):
                _ = provider.client_secret
