#!/usr/bin/env python3

import datetime
import jwt
import pytest
from cli import create_jwt_token, generate_jwt_secret
from jwt_auth import JWTValidator, AuthenticationHelper


class TestJWTValidation:
    """Simple, focused tests without mocking"""

    def setup_method(self):
        self.secret = "jwt_test_secret_12345"
        self.validator = JWTValidator(self.secret)

    def test_valid_webhook_token(self):
        token = create_jwt_token(self.secret, 'webhook', expires_in_days=1)

        is_valid, result, error = self.validator.validate_webhook_token(token)

        assert is_valid
        assert result
        assert result['scope'] == 'webhook'
        assert result['aud'] == 'webhook-endpoint'
        assert error is None

    def test_expired_token(self):
        # Create expired token
        payload = {
            'iat': datetime.datetime.utcnow() - datetime.timedelta(hours=2),
            'exp': datetime.datetime.utcnow() - datetime.timedelta(hours=1),
            'scope': 'webhook',
            'aud': 'webhook-endpoint'
        }
        token = jwt.encode(payload, self.secret[4:], algorithm='HS256')

        is_valid, result, error_msg = self.validator.validate_webhook_token(token)

        assert not is_valid
        assert "expired" in error_msg.lower()

    def test_wrong_signature(self):
        # Token signed with different secret
        wrong_payload = {
            'iat': datetime.datetime.utcnow(),
            'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1),
            'scope': 'webhook',
            'aud': 'webhook-endpoint'
        }
        token = jwt.encode(wrong_payload, "wrong_secret", algorithm='HS256')

        is_valid, result, error_msg = self.validator.validate_webhook_token(token)

        assert not is_valid
        assert "invalid" in error_msg.lower()

    def test_wrong_audience(self):
        payload = {
            'iat': datetime.datetime.utcnow(),
            'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1),
            'scope': 'webhook',
            'aud': 'wrong-audience'
        }
        token = jwt.encode(payload, self.secret[4:], algorithm='HS256')

        is_valid, result, error_msg = self.validator.validate_webhook_token(token)

        assert not is_valid
        assert "invalid" in error_msg.lower()

    def test_wrong_scope(self):
        token = create_jwt_token(self.secret, 'api', expires_in_days=1)

        is_valid, result, error_msg = self.validator.validate_webhook_token(token)

        assert not is_valid
        assert "scope" in error_msg.lower()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
