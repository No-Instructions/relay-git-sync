#!/usr/bin/env python3

import jwt
from typing import Tuple, Dict, Any, Optional


class JWTValidator:
    """JWT token validation logic extracted from web server"""

    def __init__(self, signing_secret: str):
        self.signing_secret = signing_secret
        if signing_secret.startswith("sk_"):
            self.signing_secret = signing_secret[3:]

    def validate_api_token(
        self, token: str
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """
        Validate JWT token for API endpoint

        Returns:
            (is_valid, payload, error_message)
        """
        try:
            payload = jwt.decode(
                token, self.signing_secret, algorithms=["HS256"], audience="api-endpoint"
            )

            if payload.get("scope") != "api":
                return False, None, "Invalid token scope for API endpoint"

            return True, payload, None

        except jwt.ExpiredSignatureError:
            return False, None, "Token has expired"
        except jwt.InvalidTokenError:
            return False, None, "Invalid token"
        except Exception as e:
            return False, None, f"JWT validation error: {e}"
