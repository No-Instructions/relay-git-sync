#!/usr/bin/env python3

import argparse
import os
import sys
import logging
import secrets
import base64
import jwt
import datetime
from typing import Optional
from relay_client import RelayClient
from persistence import PersistenceManager, SSHKeyManager
from sync_engine import SyncEngine
from s3rn import S3RemoteFolder

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def generate_webhook_secret():
    """Generate a secure webhook shared secret (not JWT-based)"""
    secret_bytes = secrets.token_bytes(32)
    secret_key = base64.urlsafe_b64encode(secret_bytes).decode("utf-8").rstrip("=")
    return secret_key  # No prefix - just plain shared secret


def generate_jwt_secret():
    """Generate a secure JWT signing secret"""
    secret_bytes = secrets.token_bytes(32)
    secret_key = base64.urlsafe_b64encode(secret_bytes).decode("utf-8").rstrip("=")
    return f"sk_{secret_key}"


def create_jwt_token(secret, scope, expires_in_days=30, name=None):
    """Create a JWT token with specified scope"""
    now = datetime.datetime.utcnow()
    exp = now + datetime.timedelta(days=expires_in_days)

    payload = {"iat": now, "exp": exp, "scope": scope, "aud": f"{scope}-endpoint"}

    if name:
        payload["name"] = name

    # Use the secret directly (remove prefix if present)
    signing_secret = secret
    if secret.startswith("sk_"):
        signing_secret = secret[3:]

    token = jwt.encode(payload, signing_secret, algorithm="HS256")
    return token


def sync_command(args):
    """Handle sync command"""
    try:
        # Initialize components
        relay_client = RelayClient(args.relay_server_url, args.relay_server_api_key)
        persistence_manager = PersistenceManager(args.data_dir)
        sync_engine = SyncEngine(args.data_dir, relay_client, persistence_manager)

        print(f"Syncing relay: {args.relay_id}")
        print(f"Relay server: {args.relay_server_url}")
        print(f"Data directory: {args.data_dir}")

        if args.folder_id:
            # Sync specific folder - create S3RN resource
            folder_resource = S3RemoteFolder(args.relay_id, args.folder_id)
            print(f"Syncing specific folder: {args.folder_id}")
            result = sync_engine.sync_specific_folder(folder_resource)

            if result.success:
                print(f"Successfully synced folder {args.folder_id}")
                if result.operations:
                    print(f"Performed {len(result.operations)} operations:")
                    for op in result.operations:
                        print(f"  - {op.type.value}: {op.path}")
                else:
                    print("No changes detected")
            else:
                print(f"Sync failed: {result.error}")
                return 1

        else:
            # Sync all folders
            print("Syncing all folders in relay")
            results = sync_engine.sync_relay_all_folders(args.relay_id)

            total_operations = 0
            failed_syncs = 0

            for result in results:
                if result.success:
                    print(f"✓ Synced folder {result.folder_id}")
                    if result.operations:
                        operation_count = len(result.operations)
                        total_operations += operation_count
                        print(f"  {operation_count} operations performed")
                else:
                    print(f"✗ Failed to sync folder {result.folder_id}: {result.error}")
                    failed_syncs += 1

            print(f"\nSync complete:")
            print(f"  Total operations: {total_operations}")
            print(f"  Failed folder syncs: {failed_syncs}")

            if failed_syncs > 0:
                return 1

        # Commit any changes
        print("Committing changes to git...")
        committed = persistence_manager.commit_changes()
        if committed:
            print("Changes committed successfully")
        else:
            print("No changes to commit")

        return 0

    except Exception as e:
        logger.error(f"Error during sync: {e}")
        return 1


def show_pubkey_command(args):
    """Handle show-pubkey command"""
    try:
        from persistence import SSHKeyManager

        ssh_key_manager = SSHKeyManager(args.data_dir)
        pubkey = ssh_key_manager.get_public_key()
        print("\nSSH Public Key:")
        print("=" * 50)
        print(pubkey)
        print("=" * 50)
        print(f"\nAdd this key to your Git hosting service as a deploy key.")
        print(f"Private key location: {ssh_key_manager.private_key_path_for_display}")
        return 0
    except Exception as e:
        logger.error(f"Error getting public key: {e}")
        return 1


def webhook_keygen_command(args):
    """Handle webhook keygen command"""
    try:
        # Generate shared secret (default behavior)
        secret = generate_webhook_secret()
        print("\nGenerated webhook shared secret:")
        print("=" * 50)
        print(secret)
        print("=" * 50)
        print("\nEnvironment variable:")
        print(f"export WEBHOOK_SECRET={secret}")
        return 0
    except Exception as e:
        logger.error(f"Error generating webhook secret: {e}")
        return 1


def ssh_keygen_command(args):
    """Handle ssh keygen command"""
    try:
        ssh_key_manager = SSHKeyManager(args.data_dir)
        ssh_key_manager._ensure_ssh_key()
        pubkey = ssh_key_manager.get_public_key()
        print("\nGenerated SSH Key Pair:")
        print("=" * 50)
        print(pubkey)
        print("=" * 50)
        print(f"\nAdd this key to your Git hosting service as a deploy key.")
        print(f"Private key location: {ssh_key_manager.private_key_path_for_display}")
        return 0
    except Exception as e:
        logger.error(f"Error generating SSH key: {e}")
        return 1


def api_keygen_command(args):
    """Handle api keygen command"""
    try:
        secret = generate_jwt_secret()
        print("\nGenerated API JWT signing secret:")
        print("=" * 50)
        print(secret)
        print("=" * 50)
        print("\nAdd to your environment:")
        print(f"export JWT_SECRET='{secret}'")
        return 0
    except Exception as e:
        logger.error(f"Error generating API secret: {e}")
        return 1


def api_token_create_command(args):
    """Handle api token create command"""
    try:
        jwt_secret = os.getenv("JWT_SECRET")

        if not jwt_secret:
            print("Error: JWT_SECRET environment variable is required to create API tokens.")
            print("Run: python cli.py api keygen")
            return 1

        if not jwt_secret.startswith("sk_"):
            print("Error: JWT_SECRET must start with 'sk_' prefix.")
            print("Run: python cli.py api keygen")
            return 1

        token = create_jwt_token(jwt_secret, "api", args.expires, args.name)
        print(f"\nAPI JWT Token (expires in {args.expires} days):")
        print("=" * 50)
        print(token)
        print("=" * 50)
        print("\nUse with API calls:")
        print(f"Authorization: Bearer {token}")
        return 0
    except Exception as e:
        logger.error(f"Error creating API token: {e}")
        return 1


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description="Relay Git Sync CLI - Sync collaborative documents to Git remotes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sync specific folder (folder-id is just the folder UUID)
  python cli.py sync --relay-id abc123... --folder-id def456...

  # Webhook authentication (shared secret or Svix HMAC only)
  python cli.py webhook keygen

  # API authentication (JWT tokens only)
  python cli.py api keygen
  python cli.py api token create --name "deploy-script"

  # SSH key management
  python cli.py ssh show-pubkey
  python cli.py ssh keygen

Authentication Methods:
  Webhooks: WEBHOOK_SECRET (plain shared secret or whsec_* for Svix)
  APIs:     JWT_SECRET (sk_* prefix required) + Bearer tokens
        """,
    )

    # Global arguments
    parser.add_argument(
        "--data-dir",
        default=os.getenv("RELAY_GIT_DATA_DIR", "."),
        help="Directory for repo and persistent storage (default: from RELAY_GIT_DATA_DIR env var or current directory)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Sync command
    sync_parser = subparsers.add_parser("sync", help="Sync relay documents to git")
    sync_parser.add_argument("--relay-id", required=True, help="Relay ID (UUID) to sync")
    sync_parser.add_argument(
        "--relay-server-url",
        default=os.getenv("RELAY_SERVER_URL"),
        help="Relay server URL (default: from RELAY_SERVER_URL env var)",
    )
    sync_parser.add_argument(
        "--relay-server-api-key",
        default=os.getenv("RELAY_SERVER_API_KEY"),
        help="Relay server API key (default: from RELAY_SERVER_API_KEY env var)",
    )
    sync_parser.add_argument(
        "--folder-id",
        help="Specific folder UUID to sync (optional, syncs all folders if not provided)",
    )
    sync_parser.set_defaults(func=sync_command)

    # Show pubkey command
    pubkey_parser = subparsers.add_parser("show-pubkey", help="Display SSH public key")
    pubkey_parser.set_defaults(func=show_pubkey_command)

    # Webhook command group
    webhook_parser = subparsers.add_parser("webhook", help="Webhook management")
    webhook_subparsers = webhook_parser.add_subparsers(
        dest="webhook_action", help="Webhook actions"
    )

    webhook_keygen_parser = webhook_subparsers.add_parser("keygen", help="Generate webhook secret")
    webhook_keygen_parser.set_defaults(func=webhook_keygen_command)

    # SSH command group
    ssh_parser = subparsers.add_parser("ssh", help="SSH key management")
    ssh_subparsers = ssh_parser.add_subparsers(dest="ssh_action", help="SSH actions")

    ssh_show_parser = ssh_subparsers.add_parser("show-pubkey", help="Show SSH public key")
    ssh_show_parser.set_defaults(func=show_pubkey_command)

    ssh_keygen_parser = ssh_subparsers.add_parser("keygen", help="Generate SSH key pair")
    ssh_keygen_parser.set_defaults(func=ssh_keygen_command)

    # API command group
    api_parser = subparsers.add_parser("api", help="API management")
    api_subparsers = api_parser.add_subparsers(dest="api_action", help="API actions")

    api_keygen_parser = api_subparsers.add_parser("keygen", help="Generate API JWT signing secret")
    api_keygen_parser.set_defaults(func=api_keygen_command)

    api_token_parser = api_subparsers.add_parser("token", help="API token management")
    api_token_subparsers = api_token_parser.add_subparsers(
        dest="api_token_action", help="Token actions"
    )

    api_create_parser = api_token_subparsers.add_parser("create", help="Create API token")
    api_create_parser.add_argument(
        "--expires", type=int, default=30, help="Expiration in days (default: 30)"
    )
    api_create_parser.add_argument("--name", help="Token name for identification")
    api_create_parser.set_defaults(func=api_token_create_command)

    # Parse arguments
    args = parser.parse_args()

    # Configure logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate command
    if not args.command:
        parser.print_help()
        return 1

    # Check if command has a function handler
    if not hasattr(args, "func"):
        # This means user didn't specify a subcommand for a command group
        if args.command == "webhook" and not hasattr(args, "webhook_action"):
            webhook_parser.print_help()
            return 1
        elif args.command == "ssh" and not hasattr(args, "ssh_action"):
            ssh_parser.print_help()
            return 1
        elif args.command == "api" and not hasattr(args, "api_action"):
            api_parser.print_help()
            return 1
        elif (
            args.command == "webhook"
            and args.webhook_action == "token"
            and not hasattr(args, "webhook_token_action")
        ):
            webhook_token_parser.print_help()
            return 1
        elif (
            args.command == "api"
            and args.api_action == "token"
            and not hasattr(args, "api_token_action")
        ):
            api_token_parser.print_help()
            return 1
        else:
            parser.print_help()
            return 1

    # Validate sync command requirements
    if args.command == "sync":
        if not args.relay_server_url:
            print(
                "Error: Relay server URL is required. Set RELAY_SERVER_URL environment variable or use --relay-server-url flag."
            )
            return 1

        # Validate relay ID format (basic UUID check)
        if not args.relay_id or len(args.relay_id.split("-")) != 5:
            print(
                "Error: Invalid relay ID format. Expected UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            )
            return 1

        # Validate folder ID format if provided (should be simple UUID)
        if args.folder_id and len(args.folder_id.split("-")) != 5:
            print(
                "Error: Invalid folder ID format. Expected UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            )
            return 1

    # Execute command
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        return 130  # Standard exit code for SIGINT
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
