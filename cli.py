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
from git_config import GitConnectorConfig, GitConnector

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

        ssh_key_manager = SSHKeyManager()
        pubkey = ssh_key_manager.get_public_key()
        print("\nSSH Public Key (extracted from SSH_PRIVATE_KEY):")
        print("=" * 50)
        print(pubkey)
        print("=" * 50)
        print("\nAdd this key to your Git hosting service as a deploy key.")
        return 0
    except ValueError as e:
        print(f"Error: {e}")
        print("Set SSH_PRIVATE_KEY environment variable with your private key.")
        return 1
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


def git_connector_list_command(args):
    """Handle git connector list command"""
    try:
        config_file = args.git_config_file or os.path.join(args.data_dir, "git_connectors.toml")
        git_config = GitConnectorConfig(config_file)
        
        if not git_config.connectors:
            print("No git connectors configured.")
            print(f"Create configuration file: {git_config.get_config_file_path()}")
            return 0
        
        print(f"Git Connectors ({len(git_config.connectors)} configured):")
        print("=" * 80)
        
        for i, connector in enumerate(git_config.connectors, 1):
            print(f"{i}. Relay: {connector.relay_id}")
            print(f"   Folder: {connector.shared_folder_id}")
            print(f"   URL: {connector.url}")
            print(f"   Branch: {connector.branch}")
            print(f"   Remote: {connector.remote_name}")
            if i < len(git_config.connectors):
                print()
        
        return 0
    except Exception as e:
        logger.error(f"Error listing git connectors: {e}")
        return 1


def git_connector_add_command(args):
    """Handle git connector add command"""
    try:
        config_file = args.git_config_file or os.path.join(args.data_dir, "git_connectors.toml")
        git_config = GitConnectorConfig(config_file)
        
        # Create new connector
        connector = GitConnector(
            shared_folder_id=args.folder_id,
            relay_id=args.relay_id,
            url=args.url,
            branch=args.branch,
            remote_name=args.remote_name
        )
        
        # Add to configuration
        git_config.add_connector(connector)
        
        print("Git connector added successfully:")
        print(f"  Relay ID: {connector.relay_id}")
        print(f"  Folder ID: {connector.shared_folder_id}")
        print(f"  URL: {connector.url}")
        print(f"  Branch: {connector.branch}")
        print(f"  Remote: {connector.remote_name}")
        print()
        print(f"Note: Configuration is in memory only.")
        print(f"Manually edit: {git_config.get_config_file_path()}")
        
        return 0
    except Exception as e:
        logger.error(f"Error adding git connector: {e}")
        return 1


def git_connector_remove_command(args):
    """Handle git connector remove command"""
    try:
        config_file = args.git_config_file or os.path.join(args.data_dir, "git_connectors.toml")
        git_config = GitConnectorConfig(config_file)
        
        removed = git_config.remove_connector(args.relay_id, args.folder_id)
        
        if removed:
            print("Git connector removed successfully:")
            print(f"  Relay ID: {args.relay_id}")
            print(f"  Folder ID: {args.folder_id}")
            print()
            print(f"Note: Configuration is in memory only.")
            print(f"Manually edit: {git_config.get_config_file_path()}")
        else:
            print("Git connector not found:")
            print(f"  Relay ID: {args.relay_id}")
            print(f"  Folder ID: {args.folder_id}")
            return 1
        
        return 0
    except Exception as e:
        logger.error(f"Error removing git connector: {e}")
        return 1


def git_connector_init_command(args):
    """Handle git connector init command"""
    try:
        config_file = args.git_config_file or os.path.join(args.data_dir, "git_connectors.toml")
        git_config = GitConnectorConfig(config_file)
        
        created = git_config.create_example_config()
        
        if created:
            print("Created example git connector configuration:")
            print(f"  File: {git_config.get_config_file_path()}")
            print()
            print("Edit the file to configure your git repositories.")
        else:
            print("Configuration file already exists:")
            print(f"  File: {git_config.get_config_file_path()}")
            return 1
        
        return 0
    except Exception as e:
        logger.error(f"Error creating git connector config: {e}")
        return 1


def git_connector_validate_command(args):
    """Handle git connector validate command"""
    try:
        config_file = args.git_config_file or os.path.join(args.data_dir, "git_connectors.toml")
        git_config = GitConnectorConfig(config_file)
        
        errors = git_config.validate_config()
        
        if not errors:
            print("✓ Git connector configuration is valid")
            print(f"  File: {git_config.get_config_file_path()}")
            print(f"  Connectors: {len(git_config.connectors)}")
            return 0
        else:
            print("✗ Git connector configuration has errors:")
            for error in errors:
                print(f"  - {error}")
            return 1
        
    except Exception as e:
        logger.error(f"Error validating git connector config: {e}")
        return 1


def git_connector_sync_command(args):
    """Handle git connector sync command - create repos from TOML config"""
    try:
        config_file = args.git_config_file or os.path.join(args.data_dir, "git_connectors.toml")
        persistence_manager = PersistenceManager(args.data_dir, config_file)
        
        print("Creating git repositories from TOML configuration...")
        print(f"Config file: {config_file}")
        
        # Force initialization from TOML
        initialized_count = persistence_manager._initialize_git_repos_from_toml()
        
        if initialized_count > 0:
            print(f"✓ Created {initialized_count} git repositories")
            
            # Show what was created
            for connector in persistence_manager.git_config.connectors:
                repo_key = f"{connector.relay_id}/{connector.shared_folder_id}"
                if repo_key in persistence_manager.git_repos:
                    folder_path = persistence_manager.get_folder_path(connector.relay_id, connector.shared_folder_id)
                    print(f"  - {repo_key} -> {folder_path}")
                    print(f"    Remote: {connector.remote_name} = {connector.url}")
        else:
            print("No new repositories created (may already exist)")
        
        return 0
        
    except Exception as e:
        logger.error(f"Error syncing git connectors: {e}")
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

  # Git connector management
  python cli.py git init
  python cli.py git list
  python cli.py git add --relay-id abc123... --folder-id def456... --url https://github.com/user/repo.git
  python cli.py git sync  # Create repos from TOML config

  # Webhook authentication (shared secret or Svix HMAC only)
  python cli.py webhook keygen

  # API authentication (JWT tokens only)
  python cli.py api keygen
  python cli.py api token create --name "deploy-script"

  # SSH key management
  python cli.py ssh show-pubkey

Authentication Methods:
  Webhooks: WEBHOOK_SECRET (plain shared secret or whsec_* for Svix)
  APIs:     JWT_SECRET (sk_* prefix required) + Bearer tokens

Git Connectors:
  Configure automatic git remote setup via TOML files
  File: <data-dir>/git_connectors.toml (or --git-config-file)
        """,
    )

    # Global arguments
    parser.add_argument(
        "--data-dir",
        default=os.getenv("RELAY_GIT_DATA_DIR", "."),
        help="Directory for repo and persistent storage (default: from RELAY_GIT_DATA_DIR env var or current directory)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--git-config-file",
        default=None,
        help="Path to git connectors TOML configuration file (default: <data-dir>/git_connectors.toml)",
    )

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

    # Git connector command group
    git_parser = subparsers.add_parser("git", help="Git connector management")
    git_subparsers = git_parser.add_subparsers(dest="git_action", help="Git connector actions")

    # git init command
    git_init_parser = git_subparsers.add_parser("init", help="Create example git connectors configuration")
    git_init_parser.set_defaults(func=git_connector_init_command)

    # git list command
    git_list_parser = git_subparsers.add_parser("list", help="List configured git connectors")
    git_list_parser.set_defaults(func=git_connector_list_command)

    # git add command
    git_add_parser = git_subparsers.add_parser("add", help="Add git connector configuration")
    git_add_parser.add_argument("--relay-id", required=True, help="Relay ID (UUID)")
    git_add_parser.add_argument("--folder-id", required=True, help="Shared folder ID (UUID)")
    git_add_parser.add_argument("--url", required=True, help="Git repository URL")
    git_add_parser.add_argument("--branch", default="main", help="Git branch (default: main)")
    git_add_parser.add_argument("--remote-name", default="origin", help="Git remote name (default: origin)")
    git_add_parser.set_defaults(func=git_connector_add_command)

    # git remove command
    git_remove_parser = git_subparsers.add_parser("remove", help="Remove git connector configuration")
    git_remove_parser.add_argument("--relay-id", required=True, help="Relay ID (UUID)")
    git_remove_parser.add_argument("--folder-id", required=True, help="Shared folder ID (UUID)")
    git_remove_parser.set_defaults(func=git_connector_remove_command)

    # git validate command
    git_validate_parser = git_subparsers.add_parser("validate", help="Validate git connectors configuration")
    git_validate_parser.set_defaults(func=git_connector_validate_command)

    # git sync command
    git_sync_parser = git_subparsers.add_parser("sync", help="Create git repositories from TOML configuration")
    git_sync_parser.set_defaults(func=git_connector_sync_command)

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
        elif args.command == "git" and not hasattr(args, "git_action"):
            git_parser.print_help()
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
