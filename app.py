#!/usr/bin/env python3

import argparse
import os
import logging
from relay_client import RelayClient
from persistence import PersistenceManager
from sync_engine import SyncEngine
from webhook_handler import WebhookProcessor
from operations_queue import OperationsQueue
from web_server import create_server

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def run_server(
    relay_server_url: str,
    relay_server_api_key: str,
    webhook_secret: str,
    port=8000,
    commit_interval=10,
    data_dir=".",
):
    print(f"Relay server: {relay_server_url}")
    print(f"Data directory: {data_dir}")
    print(f"Git commit interval set to {commit_interval} seconds")

    try:
        # Initialize components
        relay_client = RelayClient(relay_server_url, relay_server_api_key)
        persistence_manager = PersistenceManager(data_dir)
        sync_engine = SyncEngine(data_dir, relay_client, persistence_manager)
        webhook_processor = WebhookProcessor(relay_client)
        operations_queue = OperationsQueue(sync_engine, commit_interval)
        web_server = create_server(webhook_processor, operations_queue, webhook_secret)

        # Start the server
        web_server.run(port=port)

    except KeyboardInterrupt:
        print("\nServer shutting down...")
        # Graceful shutdown would be handled by WebServer
    except Exception as e:
        logger.error(f"Error starting server: {e}")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Relay Git Sync app")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument(
        "--commit-interval",
        type=int,
        default=10,
        help="Git commit interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--relay-server-url",
        default=os.getenv("RELAY_SERVER_URL"),
        help="Relay server URL (default: from RELAY_SERVER_URL env var)",
    )
    parser.add_argument(
        "--relay-server-api-key",
        default=os.getenv("RELAY_SERVER_API_KEY"),
        help="Relay server API key (default: from RELAY_SERVER_API_KEY env var)",
    )
    parser.add_argument(
        "--data-dir",
        default=os.getenv("RELAY_GIT_DATA_DIR", "."),
        help="Directory for repo and persistent storage (default: from RELAY_GIT_DATA_DIR env var or current directory)",
    )
    parser.add_argument(
        "--webhook-secret",
        default=os.getenv("WEBHOOK_SECRET", ""),
        help="Webhook secret for authentication (default: from WEBHOOK_SECRET env var)",
    )

    args = parser.parse_args()

    # Validate required relay server URL
    if not args.relay_server_url:
        print(
            "Error: Relay server URL is required. Set RELAY_SERVER_URL environment variable or use --relay-server-url flag."
        )
        print("For management commands (webhook, ssh, api), use: python cli.py")
        exit(1)

    # Check for SSH key (warn if missing, don't fail)
    if not os.getenv("SSH_PRIVATE_KEY"):
        print(
            "Warning: SSH_PRIVATE_KEY environment variable not set. Git push operations will fail."
        )
        print("To use Git remotes, set SSH_PRIVATE_KEY with your private key.")

    run_server(
        args.relay_server_url,
        args.relay_server_api_key,
        args.webhook_secret,
        args.port,
        args.commit_interval,
        args.data_dir,
    )
