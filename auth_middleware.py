#!/usr/bin/env python3

import logging
from typing import List, Optional, Callable
from functools import wraps
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.requests import Request
from jwt_auth import JWTValidator
from svix.webhooks import Webhook

logger = logging.getLogger(__name__)


class DefaultRejectMiddleware(BaseHTTPMiddleware):
    """Middleware to enforce default reject pattern"""

    async def dispatch(self, request, call_next):
        response = await call_next(request)

        # Check if endpoint lacks explicit auth configuration
        auth_explicitly_disabled = getattr(request.state, 'auth_explicitly_disabled', False)
        auth_explicitly_required = getattr(request.state, 'auth_explicitly_required', False)

        # If neither @noauth nor @require_auth/@webhook_auth/@api_auth was used, reject
        if not auth_explicitly_disabled and not auth_explicitly_required:
            return JSONResponse(
                {"error": "Endpoint requires explicit authentication configuration"},
                status_code=401
            )

        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """Authentication middleware with token extraction"""

    def __init__(self, app, webhook_secret: str):
        super().__init__(app)
        self.webhook_secret = webhook_secret
        self.auth_mode = self._detect_auth_mode(webhook_secret)

        # Initialize JWT validator if using JWT auth
        if self.auth_mode == 'jwt_bearer' and webhook_secret:
            self.jwt_validator = JWTValidator(webhook_secret)
        else:
            self.jwt_validator = None

    def _detect_auth_mode(self, webhook_secret):
        """Detect authentication mode based on webhook secret"""
        if not webhook_secret:
            return 'none'
        elif webhook_secret.startswith('whsec_'):
            return 'svix_hmac'
        else:
            return 'jwt_bearer'

    async def dispatch(self, request: Request, call_next):
        # Always extract and validate authentication if available
        request.state.user = None
        request.state.auth_error = None

        if self.auth_mode == 'none':
            # No authentication configured - endpoints must use @noauth
            pass
        elif self.auth_mode == 'jwt_bearer':
            await self._extract_jwt_token(request)
        elif self.auth_mode == 'svix_hmac':
            await self._validate_svix_signature(request)

        response = await call_next(request)

        # Check if endpoint lacks proper authentication decorators
        if (hasattr(response, 'status_code') and
            response.status_code == 200 and
            not getattr(request.state, 'user', None) and
            self.auth_mode != 'none'):
            # This means an endpoint responded successfully without authentication
            # and without being marked with @noauth - this should be caught by decorators
            # but this is a safety net
            pass

        return response

    async def _extract_jwt_token(self, request: Request):
        """Extract and validate JWT token"""
        try:
            auth_header = request.headers.get('Authorization')
            if not auth_header or not auth_header.startswith('Bearer '):
                return  # No token provided - let decorators handle

            token = auth_header[7:]  # Remove 'Bearer ' prefix
            if not token:
                return

            if self.jwt_validator is None:
                return

            # Store token and validator for later validation by decorators
            request.state.jwt_token = token
            request.state.jwt_validator = self.jwt_validator

        except Exception as e:
            logger.error(f"Error extracting JWT token: {e}")
            request.state.auth_error = f"Token extraction error: {e}"

    async def _validate_svix_signature(self, request: Request):
        """Validate Svix-style HMAC signature"""
        try:
            # Get required headers - check both svix- and webhook- prefixes
            svix_id = None
            svix_timestamp = None
            svix_signature = None

            for name, value in request.headers.items():
                lower_name = name.lower()
                if lower_name in ['svix-id', 'webhook-id']:
                    svix_id = value
                elif lower_name in ['svix-timestamp', 'webhook-timestamp']:
                    svix_timestamp = value
                elif lower_name in ['svix-signature', 'webhook-signature']:
                    svix_signature = value

            if svix_id is None or svix_timestamp is None or svix_signature is None:
                return  # Missing headers - let decorators handle

            # Get request body
            body = await request.body()

            # Validate using Svix SDK
            wh = Webhook(self.webhook_secret)
            wh.verify(body, {
                'svix-id': svix_id,
                'svix-timestamp': svix_timestamp,
                'svix-signature': svix_signature
            })

            # Create a minimal user object for Svix auth
            request.state.user = {
                'scope': 'webhook',
                'auth_type': 'svix',
                'svix_id': svix_id
            }

        except Exception as e:
            logger.error(f"Svix signature validation error: {e}")
            request.state.auth_error = f"Signature validation failed: {e}"


def noauth(func: Callable) -> Callable:
    """Decorator to explicitly allow unauthenticated access"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Handle both instance methods (self, request) and standalone functions (request)
        if len(args) == 2:
            # Instance method: (self, request)
            self_arg, request = args
        elif len(args) == 1:
            # Standalone function: (request)
            request = args[0]
            self_arg = None
        else:
            raise ValueError("Expected 1 or 2 positional arguments")
            
        # Mark request as explicitly allowing no auth
        request.state.auth_explicitly_disabled = True
        
        # Call function with appropriate arguments
        if self_arg is not None:
            return await func(self_arg, request)
        else:
            return await func(request)

    # Mark the function as no-auth required
    wrapper._no_auth_required = True
    return wrapper


def require_auth(scopes: Optional[List[str]] = None,
                roles: Optional[List[str]] = None,
                permissions: Optional[List[str]] = None) -> Callable:
    """
    Decorator to require authentication with optional claims matching

    Args:
        scopes: Required scopes (e.g., ['webhook', 'api'])
        roles: Required roles (e.g., ['admin', 'webhook_handler'])
        permissions: Required permissions (e.g., ['sync:read', 'sync:write'])
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Handle both instance methods (self, request) and standalone functions (request)
            if len(args) == 2:
                # Instance method: (self, request)
                self_arg, request = args
            elif len(args) == 1:
                # Standalone function: (request)
                request = args[0]
                self_arg = None
            else:
                raise ValueError("Expected 1 or 2 positional arguments")
            
            # Mark that this endpoint has explicit auth requirements
            request.state.auth_explicitly_required = True

            # Validate JWT token if present
            jwt_token = getattr(request.state, 'jwt_token', None)
            jwt_validator = getattr(request.state, 'jwt_validator', None)
            user = getattr(request.state, 'user', None)
            auth_error = getattr(request.state, 'auth_error', None)

            # If we have a JWT token but no user, validate it now with the correct scope
            if jwt_token and not user and not auth_error and jwt_validator and scopes:
                # Validate token with appropriate audience based on required scopes
                if 'webhook' in scopes:
                    is_valid, payload, error_msg = jwt_validator.validate_webhook_token(jwt_token)
                elif 'api' in scopes:
                    is_valid, payload, error_msg = jwt_validator.validate_api_token(jwt_token)
                else:
                    is_valid, payload, error_msg = False, None, "Unknown scope for JWT validation"
                
                if is_valid:
                    request.state.user = payload
                    user = payload
                else:
                    auth_error = error_msg

            if not user:
                error_detail = "Authentication required"
                if auth_error:
                    error_detail = f"Authentication failed: {auth_error}"
                return JSONResponse(
                    {"error": error_detail},
                    status_code=401
                )

            # Validate scopes
            if scopes:
                user_scope = user.get('scope')
                if not user_scope or user_scope not in scopes:
                    return JSONResponse(
                        {"error": f"Insufficient scope. Required: {scopes}, Got: {user_scope}"},
                        status_code=403
                    )

            # Validate roles
            if roles:
                user_roles = user.get('roles', [])
                if not any(role in user_roles for role in roles):
                    return JSONResponse(
                        {"error": f"Insufficient roles. Required: {roles}, Got: {user_roles}"},
                        status_code=403
                    )

            # Validate permissions
            if permissions:
                user_permissions = user.get('permissions', [])
                if not any(perm in user_permissions for perm in permissions):
                    return JSONResponse(
                        {"error": f"Insufficient permissions. Required: {permissions}, Got: {user_permissions}"},
                        status_code=403
                    )

            # Call function with appropriate arguments
            if self_arg is not None:
                return await func(self_arg, request)
            else:
                return await func(request)
        return wrapper
    return decorator


def default_reject_handler(request: Request) -> JSONResponse:
    """Default handler for endpoints without explicit auth decorators"""
    return JSONResponse(
        {"error": "Endpoint requires explicit authentication configuration"},
        status_code=401
    )


# Convenience decorators for common use cases
def webhook_auth(func: Callable) -> Callable:
    """Shorthand for webhook endpoint authentication"""
    return require_auth(scopes=['webhook'])(func)


def api_auth(roles: Optional[List[str]] = None,
            permissions: Optional[List[str]] = None) -> Callable:
    """Shorthand for API endpoint authentication"""
    return require_auth(scopes=['api'], roles=roles, permissions=permissions)
