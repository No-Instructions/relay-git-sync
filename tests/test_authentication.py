#!/usr/bin/env python3

import json
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth_middleware import AuthMiddleware, DefaultRejectMiddleware, noauth, webhook_auth, api_auth, require_auth
from cli import generate_webhook_secret, create_jwt_token
from web_server import StarletteWebServer
from webhook_handler import WebhookProcessor
from operations_queue import OperationsQueue
from relay_client import RelayClient
from sync_engine import SyncEngine
from persistence import PersistenceManager


def test_basic_middleware_functionality():
    """Test basic middleware functionality without real tokens"""

    print("Testing Basic Middleware Functionality")
    print("=" * 50)

    # Test API secret (sk_ prefix for JWT API auth)
    test_secret = "sk_dGVzdF9zZWNyZXRfa2V5XzEyMzQ1Njc4OTA="

    # Create test endpoints
    @noauth
    async def public_endpoint(request: Request):
        return JSONResponse({"message": "Public endpoint - no auth required"})

    @webhook_auth
    async def webhook_endpoint(request: Request):
        user = getattr(request.state, 'user', None)
        return JSONResponse({
            "message": "Webhook endpoint",
            "user": user
        })

    @api_auth()
    async def api_endpoint(request: Request):
        user = getattr(request.state, 'user', None)
        return JSONResponse({
            "message": "API endpoint",
            "user": user
        })

    @require_auth(scopes=['api'], roles=['admin'])
    async def admin_endpoint(request: Request):
        user = getattr(request.state, 'user', None)
        return JSONResponse({
            "message": "Admin endpoint",
            "user": user
        })

    # This endpoint has no decorator - should be rejected by default
    async def unprotected_endpoint(request: Request):
        return JSONResponse({"message": "This should be rejected"})

    # Create test app
    app = Starlette(
        routes=[
            Route('/public', public_endpoint, methods=['GET']),
            Route('/webhooks', webhook_endpoint, methods=['POST']),
            Route('/api', api_endpoint, methods=['GET']),
            Route('/admin', admin_endpoint, methods=['GET']),
            Route('/unprotected', unprotected_endpoint, methods=['GET']),
        ],
        middleware=[
            Middleware(AuthMiddleware, webhook_secret=test_secret),
            Middleware(DefaultRejectMiddleware)
        ]
    )

    client = TestClient(app)

    # Test 1: Public endpoint (should work without auth)
    print("\n1. Testing public endpoint (no auth required):")
    response = client.get("/public")
    print(f"Status: {response.status_code}")
    print(f"Response: {response.json()}")
    assert response.status_code == 200

    # Test 2: Webhook endpoint without auth (should fail)
    print("\n2. Testing webhook endpoint without auth:")
    response = client.post("/webhooks", json={"test": "data"})
    print(f"Status: {response.status_code}")
    print(f"Response: {response.json()}")
    assert response.status_code == 401

    # Note: Webhooks should NEVER accept sk_ prefixed secrets (API only)
    # So webhook endpoints will fail with this API secret, which is correct

    # Test 3: API endpoint without auth (should fail)
    print("\n3. Testing API endpoint without auth:")
    response = client.get("/api")
    print(f"Status: {response.status_code}")
    print(f"Response: {response.json()}")
    assert response.status_code == 401

    # Test 3b: API endpoint with JWT token (should work)
    print("\n3b. Testing API endpoint with JWT token:")
    api_token = create_jwt_token(test_secret, 'api', expires_in_days=1)
    headers = {"Authorization": f"Bearer {api_token}"}
    response = client.get("/api", headers=headers)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.json()}")
    assert response.status_code == 200

    # Test 4: Unprotected endpoint (should fail due to default reject)
    print("\n4. Testing unprotected endpoint (should be rejected by default):")
    response = client.get("/unprotected")
    print(f"Status: {response.status_code}")
    print(f"Response: {response.json()}")
    assert response.status_code == 401

    print("\n✅ Basic middleware functionality tests passed!")


def test_secret_generation():
    """Test webhook secret generation and character safety"""

    print("\nTesting Secret Generation")
    print("=" * 40)

    # Generate multiple secrets to ensure consistency
    secrets = [generate_webhook_secret() for _ in range(10)]

    problematic_chars = ['/', '+', '=', '"', "'", ' ', '\n', '\t', '\\']

    for i, secret in enumerate(secrets):
        # Webhook secrets no longer have prefixes - they are plain shared secrets
        has_problematic = any(char in secret for char in problematic_chars)
        print(f"Secret {i+1}: {secret[:15]}... - Safe: {not has_problematic}")
        assert not has_problematic, f"Secret {i+1} contains problematic characters"

    # Test length consistency
    lengths = [len(secret) for secret in secrets]
    assert all(length == lengths[0] for length in lengths), "Inconsistent secret lengths"

    print(f"✅ All {len(secrets)} generated secrets are environment variable safe")
    print(f"✅ All secrets have consistent length: {lengths[0]} characters")


def test_api_token_generation_and_validation():
    """Test API JWT token workflow (webhooks no longer support JWT)"""

    print("\nTesting API Token Generation and Validation")
    print("=" * 50)

    # Step 1: Generate an API secret (sk_ prefix)
    from cli import generate_jwt_secret
    api_secret = generate_jwt_secret()
    print(f"Generated API secret: {api_secret}")

    # Verify the secret has correct prefix
    assert api_secret.startswith('sk_'), f"API secret should start with 'sk_' but got: {api_secret}"

    # Verify the secret is env-var safe
    secret_part = api_secret[3:]
    problematic_chars = ['/', '+', '=', '"', "'", ' ', '\n', '\t']
    has_problematic_chars = any(char in secret_part for char in problematic_chars)
    assert not has_problematic_chars, f"Secret contains problematic characters: {secret_part}"

    # Step 2: Create API JWT tokens
    api_token_30d = create_jwt_token(api_secret, 'api', expires_in_days=30, name='test-api')
    api_token_7d = create_jwt_token(api_secret, 'api', expires_in_days=7, name='short-lived')

    print(f"API token (30d): {api_token_30d[:30]}...")
    print(f"API token (7d): {api_token_7d[:30]}...")

    # Step 3: Create test API endpoint
    @api_auth()
    async def test_api_endpoint(request: Request):
        user = getattr(request.state, 'user', None)
        return JSONResponse({
            "message": "API request successful",
            "authenticated": user is not None,
            "user": user
        })

    app = Starlette(
        routes=[
            Route('/api/test', test_api_endpoint, methods=['GET']),
        ],
        middleware=[
            Middleware(AuthMiddleware, webhook_secret=api_secret),
            Middleware(DefaultRejectMiddleware)
        ]
    )

    client = TestClient(app)

    # Test without authentication
    response = client.get("/api/test")
    assert response.status_code == 401

    # Test with valid API tokens
    headers_30d = {"Authorization": f"Bearer {api_token_30d}"}
    response = client.get("/api/test", headers=headers_30d)
    assert response.status_code == 200
    response_data = response.json()
    assert response_data["authenticated"] == True
    assert response_data["user"]["scope"] == "api"
    assert response_data["user"]["name"] == "test-api"

    headers_7d = {"Authorization": f"Bearer {api_token_7d}"}
    response = client.get("/api/test", headers=headers_7d)
    assert response.status_code == 200
    response_data = response.json()
    assert response_data["user"]["name"] == "short-lived"

    # Test edge cases
    malformed_headers = {"Authorization": "Bearer invalid.jwt.token"}
    response = client.get("/api/test", headers=malformed_headers)
    assert response.status_code == 401

    no_bearer_headers = {"Authorization": api_token_30d}
    response = client.get("/api/test", headers=no_bearer_headers)
    assert response.status_code == 401

    print("✅ API token generation and validation tests passed!")


def test_flexible_authentication():
    """Test that authentication works based on decorators, not hardcoded URL paths"""

    print("\nTesting Flexible Authentication")
    print("=" * 40)

    # Generate secrets
    webhook_secret = generate_webhook_secret()  # Plain shared secret
    from cli import generate_jwt_secret
    api_secret = generate_jwt_secret()  # sk_ prefixed secret for JWT
    api_token = create_jwt_token(api_secret, 'api', expires_in_days=1)

    # Create endpoints with different paths but same authentication requirements
    @webhook_auth
    async def webhook_handler(request: Request):
        user = getattr(request.state, 'user', None)
        return JSONResponse({"type": "webhook", "user": user})

    @webhook_auth
    async def another_webhook_handler(request: Request):
        user = getattr(request.state, 'user', None)
        return JSONResponse({"type": "another_webhook", "user": user})

    @api_auth()
    async def api_handler(request: Request):
        user = getattr(request.state, 'user', None)
        return JSONResponse({"type": "api", "user": user})

    @api_auth()
    async def different_api_handler(request: Request):
        user = getattr(request.state, 'user', None)
        return JSONResponse({"type": "different_api", "user": user})

    # Create separate apps to test webhook and API auth independently
    # Note: In practice, you'd typically use one secret type per service

    # Test webhook endpoints with shared secret auth
    webhook_app = Starlette(
        routes=[
            Route('/webhooks', webhook_handler, methods=['POST']),
            Route('/events/incoming', another_webhook_handler, methods=['POST']),
        ],
        middleware=[
            Middleware(AuthMiddleware, webhook_secret=webhook_secret),
            Middleware(DefaultRejectMiddleware)
        ]
    )

    # Test API endpoints with JWT auth
    api_app = Starlette(
        routes=[
            Route('/api/sync', api_handler, methods=['GET']),
            Route('/management/status', different_api_handler, methods=['GET']),
        ],
        middleware=[
            Middleware(AuthMiddleware, webhook_secret=api_secret),
            Middleware(DefaultRejectMiddleware)
        ]
    )

    webhook_client = TestClient(webhook_app)
    api_client = TestClient(api_app)

    # Test webhook endpoints with shared secret
    webhook_headers = {"Authorization": f"Bearer {webhook_secret}"}

    response = webhook_client.post("/webhooks", json={}, headers=webhook_headers)
    assert response.status_code == 200
    assert response.json()["type"] == "webhook"

    response = webhook_client.post("/events/incoming", json={}, headers=webhook_headers)
    assert response.status_code == 200
    assert response.json()["type"] == "another_webhook"

    # Test API endpoints with API JWT token
    api_headers = {"Authorization": f"Bearer {api_token}"}

    response = api_client.get("/api/sync", headers=api_headers)
    assert response.status_code == 200
    assert response.json()["type"] == "api"

    response = api_client.get("/management/status", headers=api_headers)
    assert response.status_code == 200
    assert response.json()["type"] == "different_api"

    # Test authentication failures
    response = webhook_client.post("/webhooks", json={})  # No auth
    assert response.status_code == 401

    response = api_client.get("/api/sync")  # No auth
    assert response.status_code == 401

    print("✅ Flexible authentication tests passed!")


def test_web_server_integration():
    """Integration test with actual web server components"""

    print("\nTesting Web Server Integration")
    print("=" * 40)

    # Create minimal components for testing
    relay_client = RelayClient("http://test", "test-key")
    persistence_manager = PersistenceManager("/tmp/test")
    sync_engine = SyncEngine("/tmp/test", relay_client, persistence_manager)
    webhook_processor = WebhookProcessor(relay_client)
    operations_queue = OperationsQueue(sync_engine, commit_interval=10)
    webhook_secret = "test_shared_secret_123"

    # Create the actual web server
    server = StarletteWebServer(webhook_processor, operations_queue, webhook_secret)
    client = TestClient(server.app)

    # Test health endpoint (should work without auth)
    response = client.get("/health")
    assert response.status_code == 200

    # Test webhook endpoint without auth (should fail)
    response = client.post("/webhooks", json={"test": "data"})
    assert response.status_code == 401

    print("✅ Web server integration tests passed!")


if __name__ == "__main__":
    test_basic_middleware_functionality()
    test_secret_generation()
    test_api_token_generation_and_validation()
    test_flexible_authentication()
    test_web_server_integration()
