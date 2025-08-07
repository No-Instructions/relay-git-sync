#!/usr/bin/env python3

import jwt
from typing import Tuple, Dict, Any, Optional


class JWTValidator:
    """JWT token validation logic extracted from web server"""

    def __init__(self, signing_secret: str):
        self.signing_secret = signing_secret
        if signing_secret.startswith(('whs_', 'jwt_')):
            self.signing_secret = signing_secret[4:]

    def validate_webhook_token(self, token: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """
        Validate JWT token for webhook endpoint

        Returns:
            (is_valid, payload, error_message)
        """
        try:
            payload = jwt.decode(
                token,
                self.signing_secret,
                algorithms=['HS256'],
                audience='webhook-endpoint'
            )

            if payload.get('scope') != 'webhook':
                return False, None, f"Invalid scope. Expected 'webhook', got '{payload.get('scope')}'"

            return True, payload, None

        except jwt.ExpiredSignatureError:
            return False, None, "Token has expired"
        except jwt.InvalidAudienceError:
            return False, None, "Invalid scope. Expected 'webhook', got token for different endpoint"
        except jwt.InvalidTokenError:
            return False, None, "Invalid token"
        except Exception as e:
            return False, None, f"JWT validation error: {e}"

    def validate_api_token(self, token: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """
        Validate JWT token for API endpoint

        Returns:
            (is_valid, payload, error_message)
        """
        try:
            payload = jwt.decode(
                token,
                self.signing_secret,
                algorithms=['HS256'],
                audience='api-endpoint'
            )

            if payload.get('scope') != 'api':
                return False, None, "Invalid token scope for API endpoint"

            return True, payload, None

        except jwt.ExpiredSignatureError:
            return False, None, "Token has expired"
        except jwt.InvalidTokenError:
            return False, None, "Invalid token"
        except Exception as e:
            return False, None, f"JWT validation error: {e}"


class AuthenticationHelper:
    """Helper class for HTTP authentication logic"""

    def __init__(self, jwt_validator: JWTValidator):
        self.jwt_validator = jwt_validator

    def extract_bearer_token(self, auth_header: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract Bearer token from Authorization header

        Returns:
            (success, token, error_message)
        """
        if not auth_header:
            return None, "Missing Authorization header"

        if not auth_header.startswith('Bearer '):
            return None, "Invalid Authorization header format"

        token = auth_header[7:]  # Remove 'Bearer ' prefix
        if not token:
            return None, "Empty token in Authorization header"

        return token, None

    def validate_request_token(self, auth_header: Optional[str], endpoint_path: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """
        Complete token validation for HTTP request

        Returns:
            (is_valid, payload, error_message)
        """
        # Extract token
        token, error = self.extract_bearer_token(auth_header)
        if token is None:
            return False, None, error

        # Validate based on endpoint
        if endpoint_path == '/webhook':
            return self.jwt_validator.validate_webhook_token(token)
        else:
            return self.jwt_validator.validate_api_token(token)
