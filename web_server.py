#!/usr/bin/env python3

import json
import logging
import traceback
import os

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route
import uvicorn
import yaml

from webhook_handler import WebhookProcessor
from operations_queue import OperationsQueue
from auth_middleware import AuthMiddleware, DefaultRejectMiddleware, noauth, webhook_auth
from persistence import PersistenceManager, SSHKeyManager

logger = logging.getLogger(__name__)


class StarletteWebServer:
    """Starlette-based webhook server with default reject authentication"""

    def __init__(
        self,
        webhook_processor: WebhookProcessor,
        operations_queue: OperationsQueue,
        webhook_secret: str,
        persistence_manager: PersistenceManager,
    ):
        self.webhook_processor = webhook_processor
        self.operations_queue = operations_queue
        self.webhook_secret = webhook_secret
        self.persistence_manager = persistence_manager

        # Create middleware with default reject pattern
        middleware = [
            Middleware(AuthMiddleware, webhook_secret=webhook_secret),
            Middleware(DefaultRejectMiddleware),
        ]

        # Create Starlette app
        self.app = Starlette(
            routes=[
                Route("/webhooks", self.handle_webhook, methods=["POST"]),
                Route("/health", self.health_check, methods=["GET"]),
                Route("/api/pubkey", self.get_pubkey, methods=["GET"]),
                Route("/docs", self.api_docs, methods=["GET"]),
                Route("/openapi.yaml", self.openapi_spec, methods=["GET"]),
            ],
            middleware=middleware,
        )

    @webhook_auth
    async def handle_webhook(self, request: Request):
        """Handle webhook POST requests - requires webhook authentication"""
        try:
            # Get request body
            body = await request.body()

            # Parse JSON payload
            try:
                webhook_data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.error(f"Invalid JSON payload: {e}")
                return JSONResponse({"error": "Invalid JSON payload"}, status_code=400)

            # Process webhook
            logger.info(f"Processing webhook: {webhook_data}")

            # Extract payload and process through WebhookProcessor
            payload = webhook_data.get("payload", {})
            change_data = self.webhook_processor.process_webhook(payload)

            if change_data is None:
                logger.error("Failed to process webhook payload")
                return JSONResponse({"error": "Invalid webhook payload"}, status_code=400)

            # Queue the processed webhook data
            self.operations_queue.enqueue_document_change(change_data)

            return JSONResponse({"status": "received"})

        except Exception as e:
            logger.error(f"Error processing webhook: {e}")
            logger.error(f"Webhook processing traceback: {traceback.format_exc()}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

    @noauth
    async def health_check(self, request: Request):
        """Health check endpoint - no authentication required"""
        return JSONResponse({"status": "healthy"})

    @noauth
    async def get_pubkey(self, request: Request):
        """Get SSH public key - no authentication required"""
        try:
            # Check if SSH key manager is available
            if not self.persistence_manager.ssh_key_manager:
                return JSONResponse(
                    {"error": "SSH_PRIVATE_KEY environment variable not configured"},
                    status_code=400,
                )

            # Get the public key
            public_key = self.persistence_manager.ssh_key_manager.get_public_key()

            # Determine key type from the public key string
            key_type = "unknown"
            if public_key.startswith("ssh-rsa"):
                key_type = "ssh-rsa"
            elif public_key.startswith("ssh-ed25519"):
                key_type = "ssh-ed25519"
            elif public_key.startswith("ecdsa-sha2-"):
                key_type = "ecdsa"

            return JSONResponse({"public_key": public_key, "key_type": key_type})

        except Exception as e:
            logger.error(f"Error getting public key: {e}")
            return JSONResponse(
                {"error": f"Failed to retrieve public key: {str(e)}"}, status_code=500
            )

    @noauth
    async def api_docs(self, request: Request):
        """Serve interactive Swagger UI documentation - no authentication required"""
        try:
            # Get the base URL for the API spec, respecting forwarded protocol headers
            base_url = str(request.base_url).rstrip("/")

            # Use X-Forwarded-Proto header if present (common with reverse proxies)
            forwarded_proto = request.headers.get("x-forwarded-proto")
            if forwarded_proto == "https" and base_url.startswith("http://"):
                base_url = base_url.replace("http://", "https://")

            openapi_url = f"{base_url}/openapi.yaml"

            # Create Swagger UI HTML using CDN
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Relay Git Sync API Documentation</title>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <link rel="stylesheet" type="text/css" href="https://unpkg.com/swagger-ui-dist@5.10.5/swagger-ui.css" />
                <style>
                    html {{ box-sizing: border-box; overflow: -moz-scrollbars-vertical; overflow-y: scroll; }}
                    *, *:before, *:after {{ box-sizing: inherit; }}
                    body {{ margin:0; background: #fafafa; }}
                </style>
            </head>
            <body>
                <div id="swagger-ui"></div>
                <script src="https://unpkg.com/swagger-ui-dist@5.10.5/swagger-ui-bundle.js"></script>
                <script src="https://unpkg.com/swagger-ui-dist@5.10.5/swagger-ui-standalone-preset.js"></script>
                <script>
                    function CustomTopbarPlugin() {{
                        // this plugin overrides the Topbar component to return nothing
                        return {{
                            components: {{
                                Topbar: () => null
                            }}
                        }}
                    }}

                    window.onload = function() {{
                        const ui = SwaggerUIBundle({{
                            url: '{openapi_url}',
                            dom_id: '#swagger-ui',
                            deepLinking: true,
                            presets: [
                                SwaggerUIBundle.presets.apis,
                                SwaggerUIStandalonePreset
                            ],
                            plugins: [
                                SwaggerUIBundle.plugins.DownloadUrl,
                                CustomTopbarPlugin
                            ],
                            layout: "StandaloneLayout"
                        }});
                    }};
                </script>
            </body>
            </html>
            """

            return HTMLResponse(html_content)

        except Exception as e:
            logger.error(f"Error serving API docs: {e}")
            return HTMLResponse(
                f"<html><body><h1>Error loading API documentation</h1><p>{str(e)}</p></body></html>",
                status_code=500,
            )

    @noauth
    async def openapi_spec(self, request: Request):
        """Serve OpenAPI specification - no authentication required"""
        try:
            # Get the base URL, respecting forwarded protocol headers
            base_url = str(request.base_url).rstrip("/")

            # Use X-Forwarded-Proto header if present (common with reverse proxies)
            forwarded_proto = request.headers.get("x-forwarded-proto")
            if forwarded_proto == "https" and base_url.startswith("http://"):
                base_url = base_url.replace("http://", "https://")

            # Define OpenAPI spec inline to avoid deployment issues
            spec_data = {
                "openapi": "3.0.3",
                "info": {
                    "title": "Relay Git Sync API",
                    "description": "REST API for Relay Git Sync service - public endpoints for SSH key management and service health.",
                    "version": "1.0.0",
                    "contact": {"name": "System 3"},
                    "license": {"name": "MIT"},
                },
                "servers": [
                    {"url": base_url, "description": "Current server"},
                    {"url": "http://localhost:8000", "description": "Local development server"},
                ],
                "paths": {
                    "/health": {
                        "get": {
                            "summary": "Health check",
                            "description": "Returns the current health status of the service",
                            "tags": ["Health"],
                            "security": [],
                            "responses": {
                                "200": {
                                    "description": "Service is healthy",
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "properties": {
                                                    "status": {
                                                        "type": "string",
                                                        "example": "healthy",
                                                    }
                                                },
                                                "required": ["status"],
                                            }
                                        }
                                    },
                                }
                            },
                        }
                    },
                    "/api/pubkey": {
                        "get": {
                            "summary": "Get SSH public key",
                            "description": "Returns the SSH public key used for Git operations. This endpoint does not require authentication and can be used by CI/CD systems to programmatically retrieve the public key for Git hosting service configuration.",
                            "tags": ["SSH Keys"],
                            "security": [],
                            "responses": {
                                "200": {
                                    "description": "SSH public key retrieved successfully",
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "properties": {
                                                    "public_key": {
                                                        "type": "string",
                                                        "description": "The SSH public key in OpenSSH format",
                                                        "example": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyDataHereForTesting",
                                                    },
                                                    "key_type": {
                                                        "type": "string",
                                                        "description": "The type of SSH key",
                                                        "enum": [
                                                            "ssh-rsa",
                                                            "ssh-ed25519",
                                                            "ecdsa",
                                                            "unknown",
                                                        ],
                                                        "example": "ssh-ed25519",
                                                    },
                                                },
                                                "required": ["public_key", "key_type"],
                                            }
                                        }
                                    },
                                },
                                "400": {
                                    "description": "SSH private key not configured",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "#/components/schemas/Error"},
                                            "example": {
                                                "error": "SSH_PRIVATE_KEY environment variable not configured"
                                            },
                                        }
                                    },
                                },
                                "500": {
                                    "description": "Internal server error retrieving public key",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "#/components/schemas/Error"},
                                            "example": {
                                                "error": "Failed to retrieve public key: Invalid key format"
                                            },
                                        }
                                    },
                                },
                            },
                        }
                    },
                },
                "components": {
                    "schemas": {
                        "Error": {
                            "type": "object",
                            "properties": {
                                "error": {
                                    "type": "string",
                                    "description": "Human-readable error message",
                                }
                            },
                            "required": ["error"],
                        }
                    }
                },
                "tags": [
                    {"name": "Health", "description": "Service health and status endpoints"},
                    {"name": "SSH Keys", "description": "SSH key management and retrieval"},
                ],
            }

            # Return as YAML with proper content type
            yaml_content = yaml.dump(spec_data, default_flow_style=False, sort_keys=False)

            from starlette.responses import Response

            return Response(
                yaml_content,
                media_type="application/x-yaml",
                headers={"Content-Disposition": "inline; filename=openapi.yaml"},
            )

        except Exception as e:
            logger.error(f"Error serving OpenAPI spec: {e}")
            return JSONResponse(
                {"error": f"Failed to load OpenAPI specification: {str(e)}"}, status_code=500
            )

    def run(self, host: str = "0.0.0.0", port: int = 8000):
        """Run the server"""
        auth_mode = "disabled"
        if self.webhook_secret:
            if self.webhook_secret.startswith("whsec_"):
                auth_mode = "Svix HMAC signature validation"
            else:
                auth_mode = "Shared secret validation (exact match)"

        logger.info(f"Starting Starlette webhook server on {host}:{port}")
        logger.info(f"Authentication: {auth_mode}")

        uvicorn.run(self.app, host=host, port=port, log_level="info")


def create_server(
    webhook_processor: WebhookProcessor,
    operations_queue: OperationsQueue,
    webhook_secret: str,
    persistence_manager: PersistenceManager,
) -> StarletteWebServer:
    """Create and configure the Starlette webhook server"""
    return StarletteWebServer(
        webhook_processor, operations_queue, webhook_secret, persistence_manager
    )
