#!/usr/bin/env python3

import json
import logging
import traceback

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from webhook_handler import WebhookProcessor
from operations_queue import OperationsQueue
from auth_middleware import AuthMiddleware, DefaultRejectMiddleware, noauth, webhook_auth

logger = logging.getLogger(__name__)


class StarletteWebServer:
    """Starlette-based webhook server with default reject authentication"""

    def __init__(self, webhook_processor: WebhookProcessor, operations_queue: OperationsQueue, webhook_secret: str):
        self.webhook_processor = webhook_processor
        self.operations_queue = operations_queue
        self.webhook_secret = webhook_secret

        # Create middleware with default reject pattern
        middleware = [
            Middleware(AuthMiddleware, webhook_secret=webhook_secret),
            Middleware(DefaultRejectMiddleware)
        ]

        # Create Starlette app
        self.app = Starlette(
            routes=[
                Route('/webhooks', self.handle_webhook, methods=['POST']),
                Route('/health', self.health_check, methods=['GET']),
            ],
            middleware=middleware
        )

    @webhook_auth
    async def handle_webhook(self, request: Request):
        """Handle webhook POST requests - requires webhook authentication"""
        try:
            # Get request body
            body = await request.body()

            # Parse JSON payload
            try:
                webhook_data = json.loads(body.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.error(f"Invalid JSON payload: {e}")
                return JSONResponse(
                    {"error": "Invalid JSON payload"},
                    status_code=400
                )

            # Process webhook
            logger.info(f"Processing webhook: {webhook_data}")

            # Extract payload and process through WebhookProcessor
            payload = webhook_data.get("payload", {})
            change_data = self.webhook_processor.process_webhook(payload)
            
            if change_data is None:
                logger.error("Failed to process webhook payload")
                return JSONResponse(
                    {"error": "Invalid webhook payload"},
                    status_code=400
                )

            # Queue the processed webhook data
            self.operations_queue.enqueue_document_change(change_data)

            return JSONResponse({"status": "received"})

        except Exception as e:
            logger.error(f"Error processing webhook: {e}")
            logger.error(f"Webhook processing traceback: {traceback.format_exc()}")
            return JSONResponse(
                {"error": "Internal server error"},
                status_code=500
            )

    @noauth
    async def health_check(self, request: Request):
        """Health check endpoint - no authentication required"""
        return JSONResponse({"status": "healthy"})

    def run(self, host: str = "0.0.0.0", port: int = 8000):
        """Run the server"""
        auth_mode = "disabled"
        if self.webhook_secret:
            if self.webhook_secret.startswith('whsec_'):
                auth_mode = "Svix HMAC signature validation"
            else:
                auth_mode = "JWT bearer token validation"

        logger.info(f"Starting Starlette webhook server on {host}:{port}")
        logger.info(f"Authentication: {auth_mode}")

        uvicorn.run(self.app, host=host, port=port, log_level="info")


def create_server(webhook_processor: WebhookProcessor, operations_queue: OperationsQueue, webhook_secret: str) -> StarletteWebServer:
    """Create and configure the Starlette webhook server"""
    return StarletteWebServer(webhook_processor, operations_queue, webhook_secret)
