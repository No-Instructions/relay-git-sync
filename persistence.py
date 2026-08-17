#!/usr/bin/env python3

import atexit
import glob
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

import git

from git_config import GitConnectorConfig, default_git_config_file
from models import get_s3rn_resource_category
from s3rn import (
    S3RN,
    ResourceInterface,
    S3RemoteCanvas,
    S3RemoteDocument,
    S3RemoteFile,
    S3RemoteFolder,
    S3RNType,
)

logger = logging.getLogger(__name__)


class SSHKeyManager:
    """Manage SSH keys for git authentication using key files"""

    def __init__(
        self,
        data_dir: str = ".",
        ssh_key_env_var: str = "SSH_PRIVATE_KEY",
        known_hosts: Optional[List[str]] = None,
    ):
        self.data_dir = data_dir
        self.ssh_key_env_var = ssh_key_env_var
        self.known_hosts = known_hosts or []
        self.ssh_dir = os.path.join(data_dir, "ssh")
        self.private_key_path = os.path.join(self.ssh_dir, "git_sync_key")
        self.public_key_path = os.path.join(self.ssh_dir, "git_sync_key.pub")
        self.known_hosts_path = os.path.join(self.ssh_dir, "known_hosts")

        self._setup_ssh_keys()

    def _setup_ssh_keys(self):
        """Set up SSH keys from environment variable"""
        ssh_key = os.environ.get(self.ssh_key_env_var)
        if not ssh_key:
            raise ValueError(f"Environment variable {self.ssh_key_env_var} not found")

        try:
            # Shell command substitution and several secret stores strip the
            # trailing newline from multiline values. OpenSSH's libcrypto
            # loader rejects otherwise-valid private keys without it.
            normalized_ssh_key = ssh_key.rstrip("\r\n") + "\n"

            # Create SSH directory
            os.makedirs(self.ssh_dir, mode=0o700, exist_ok=True)

            # Write private key to file
            with open(self.private_key_path, "w") as f:
                f.write(normalized_ssh_key)
            os.chmod(self.private_key_path, 0o600)  # Read-only for owner

            # Generate and write public key
            public_key = self._extract_public_key(normalized_ssh_key)
            with open(self.public_key_path, "w") as f:
                f.write(public_key + "\n")
            os.chmod(self.public_key_path, 0o644)  # Read for owner and group

            if self.known_hosts:
                with open(self.known_hosts_path, "w") as f:
                    f.write("\n".join(self.known_hosts))
                    f.write("\n")
                os.chmod(self.known_hosts_path, 0o644)

            logger.info(f"SSH keys written to {self.ssh_dir}")
            logger.info(f"Public key: {public_key[:50]}...")
            if self.known_hosts:
                logger.info(f"Known hosts written to {self.known_hosts_path}")

        except Exception as e:
            raise RuntimeError(f"Failed to setup SSH keys: {e}")

    def _extract_public_key(self, private_key_pem: str) -> str:
        """Extract public key from private key"""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        try:
            # Load private key
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None, backend=default_backend()
            )

            # Extract public key
            public_key = private_key.public_key()
            public_ssh = public_key.public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            )

            return public_ssh.decode("utf-8").strip()
        except Exception as e:
            # Try to parse as OpenSSH private key format
            try:
                from cryptography.hazmat.primitives.serialization import load_ssh_private_key

                private_key = load_ssh_private_key(
                    private_key_pem.encode("utf-8"), password=None, backend=default_backend()
                )

                # Extract public key
                public_key = private_key.public_key()
                public_ssh = public_key.public_bytes(
                    encoding=serialization.Encoding.OpenSSH,
                    format=serialization.PublicFormat.OpenSSH,
                )

                return public_ssh.decode("utf-8").strip()
            except Exception:
                raise ValueError(f"Invalid private key format: {str(e)}")

    def get_public_key(self) -> str:
        """Get public key from file"""
        try:
            with open(self.public_key_path, "r") as f:
                return f.read().strip()
        except Exception as e:
            raise ValueError(f"Could not read public key file: {e}")


class PersistenceManager:
    """Manage file-based state and git repository operations"""

    HASHES_FILE = "document_hashes.json"
    SUBDOC_HEADS_FILE = "subdoc_heads.json"
    FILEMETA_FILE = "shared_folders.json"
    MIRROR_BASE_DIR = "repos"
    LOCAL_STATE_FILE = "local_state.json"
    UNPUSHED_CHECK_FETCH_INTERVAL_SECONDS = 300

    def __init__(self, data_dir: str = ".", git_config_file: Optional[str] = None):
        self.data_dir = data_dir
        self.git_repos: Dict[str, git.Repo] = {}  # Now keyed by "relay_id/folder_id"
        self.git_lock = threading.Lock()  # Prevent concurrent git operations
        # Last fetch time per repo for the unpushed-commit check, keyed by repo_key
        self._unpushed_check_fetch_times: Dict[str, float] = {}

        # Initialize git connector configuration first to get known hosts
        config_path = default_git_config_file(self.data_dir, git_config_file)
        self.git_config = GitConnectorConfig(config_path)
        config_errors = self.git_config.validate_config()
        if config_errors:
            raise ValueError("Invalid git connector config: " + "; ".join(config_errors))

        # Initialize SSH key manager only if SSH_PRIVATE_KEY is set
        self.ssh_key_manager = None
        ssh_private_key = os.getenv("SSH_PRIVATE_KEY")
        if ssh_private_key:
            print(f"SSH_PRIVATE_KEY found, length: {len(ssh_private_key)} chars")
            try:
                self.ssh_key_manager = SSHKeyManager(
                    self.data_dir,
                    "SSH_PRIVATE_KEY",
                    known_hosts=self.git_config.known_hosts,
                )
                print("SSH key manager initialized successfully")
                # Set up SSH environment globally for all Git operations
                self._setup_global_ssh_environment()
            except Exception as e:
                logger.error(f"Failed to initialize SSH key manager: {e}")
                logger.error("Git operations requiring SSH authentication will fail")
                print(f"SSH key manager initialization failed: {e}")
        else:
            print("SSH_PRIVATE_KEY environment variable not found")
            logger.warning("SSH_PRIVATE_KEY not set - SSH Git operations will fail")

        # In-memory storage for state (these will be loaded/saved per relay)
        self.document_hashes: Dict[str, Dict[str, str]] = {}  # keyed by relay_id then doc_id
        self.subdoc_heads: Dict[str, Dict[str, str]] = {}  # keyed by relay_id then doc_id
        self.filemeta_folders: Dict[str, Dict[str, Dict]] = (
            {}
        )  # keyed by relay_id then folder doc_id
        self.local_file_state: Dict[str, Dict[str, Dict]] = (
            {}
        )  # keyed by relay_id then folder_id then path

        # In-memory resource index (built from existing data sources)
        self.resource_index: Dict[str, Dict[str, Dict]] = {}  # keyed by relay_id then resource_id
        self.resource_index_lock = threading.RLock()  # Thread safety for resource index

        # Setup graceful shutdown
        self._setup_signal_handlers()
        # Clean up any stale git lock files from previous crashes
        self._cleanup_git_lock_files()

        # Initialize Git repositories for all existing folders
        self._initialize_all_git_repos()

    def get_state_dir(self, relay_id: str) -> str:
        """Get state directory for a specific relay"""
        return os.path.join(self.data_dir, "state", relay_id)

    def get_hashes_file_path(self, relay_id: str) -> str:
        return os.path.join(self.get_state_dir(relay_id), self.HASHES_FILE)

    def get_subdoc_heads_file_path(self, relay_id: str) -> str:
        return os.path.join(self.get_state_dir(relay_id), self.SUBDOC_HEADS_FILE)

    def get_filemeta_file_path(self, relay_id: str) -> str:
        return os.path.join(self.get_state_dir(relay_id), self.FILEMETA_FILE)

    def get_repo_dir(self, resource_or_relay_id) -> str:
        """Get repository directory from S3RN resource or relay_id string"""
        if isinstance(resource_or_relay_id, str):
            # Legacy string relay_id
            relay_id = resource_or_relay_id
        else:
            # S3RN resource
            relay_id = S3RN.get_relay_id(resource_or_relay_id)

        return os.path.join(self.data_dir, self.MIRROR_BASE_DIR, relay_id)

    def get_local_state_file_path(self, relay_id: str) -> str:
        return os.path.join(self.get_state_dir(relay_id), self.LOCAL_STATE_FILE)

    def _setup_signal_handlers(self):
        """Setup graceful shutdown handlers"""

        def shutdown_handler(signum, frame):
            logger.info(f"Received signal {signum}, cleaning up...")
            self._cleanup_git_lock_files()
            exit(0)

        # Register signal handlers
        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)
        # Register atexit handler as well
        atexit.register(self._cleanup_git_lock_files)

    def _cleanup_git_lock_files(self):
        """Clean up stale git lock files from all repositories"""
        try:
            repos_dir = os.path.join(self.data_dir, self.MIRROR_BASE_DIR)
            if not os.path.exists(repos_dir):
                return

            logger.info("Cleaning up stale git lock files...")
            cleaned_count = 0

            # Find all git lock files recursively
            lock_patterns = [
                "**/.git/index.lock",
                "**/.git/config.lock",
                "**/.git/refs/heads/*.lock",
                "**/.git/refs/remotes/*/*.lock",
                "**/.git/HEAD.lock",
            ]

            for pattern in lock_patterns:
                lock_files = glob.glob(os.path.join(repos_dir, pattern), recursive=True)
                for lock_file in lock_files:
                    try:
                        if os.path.exists(lock_file):
                            os.remove(lock_file)
                            cleaned_count += 1
                            logger.info(f"Removed stale git lock file: {lock_file}")
                    except Exception as e:
                        logger.warning(f"Could not remove lock file {lock_file}: {e}")

            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} stale git lock files")
            else:
                logger.debug("No stale git lock files found")

        except Exception as e:
            logger.error(f"Error during git lock file cleanup: {e}")

    def _initialize_all_git_repos(self):
        """Initialize Git repositories for configured folders."""
        try:
            logger.info("Initializing Git repositories...")
            initialized_count = 0

            # First: Initialize repos for configured folders that already have state.
            state_base_dir = os.path.join(self.data_dir, "state")
            if os.path.exists(state_base_dir):
                # Scan all relay state directories
                for relay_id in os.listdir(state_base_dir):
                    relay_state_dir = os.path.join(state_base_dir, relay_id)
                    if not os.path.isdir(relay_state_dir):
                        continue

                    # Load persistent data to get folder information
                    self.load_persistent_data(relay_id)

                    # Initialize Git repos for all folders in this relay
                    filemeta_folders = self.filemeta_folders.get(relay_id, {})
                    for folder_id in filemeta_folders.keys():
                        if not self.should_sync_folder(relay_id, folder_id):
                            logger.info(
                                f"Skipping unconfigured persisted folder {relay_id}/{folder_id}"
                            )
                            continue

                        try:
                            self.init_git_repo(relay_id, folder_id)
                            # Auto-configure git remote if connector exists
                            self._auto_configure_git_remote(relay_id, folder_id)
                            initialized_count += 1
                        except Exception as e:
                            logger.error(
                                f"Failed to initialize Git repo for folder {folder_id} in relay {relay_id}: {e}"
                            )

            # Second: Initialize repos from TOML configuration (even without prior state)
            toml_initialized_count = self._initialize_git_repos_from_toml()
            initialized_count += toml_initialized_count

            if initialized_count > 0:
                logger.info(f"Initialized {initialized_count} Git repositories total")
            else:
                logger.debug("No folders found to initialize")

        except Exception as e:
            logger.error(f"Error during Git repository initialization: {e}")

    def is_folder_configured(self, relay_id: str, folder_id: str) -> bool:
        return self.git_config.get_connector_for_folder(relay_id, folder_id) is not None

    def should_sync_folder(self, relay_id: str, folder_id: str) -> bool:
        if not self.git_config.connectors:
            return True
        return self.is_folder_configured(relay_id, folder_id)

    def _configured_folder_ids(self, relay_id: str) -> set[str]:
        return {
            connector.shared_folder_id
            for connector in self.git_config.get_connectors_for_relay(relay_id)
        }

    def _is_repo_key_configured(self, repo_key: str) -> bool:
        try:
            relay_id, folder_id = repo_key.split("/", 1)
        except ValueError:
            return False
        return self.should_sync_folder(relay_id, folder_id)

    def _initialize_git_repos_from_toml(self) -> int:
        """Initialize Git repositories from TOML configuration, creating minimal state as needed"""
        try:
            if not self.git_config.connectors:
                logger.debug("No git connectors configured in TOML")
                return 0

            logger.info("Initializing Git repositories from TOML configuration...")
            initialized_count = 0

            for connector in self.git_config.connectors:
                try:
                    relay_id = connector.relay_id
                    folder_id = connector.shared_folder_id

                    # Check if this repo already exists
                    repo_key = f"{relay_id}/{folder_id}"
                    if repo_key in self.git_repos:
                        logger.debug(f"Git repo already exists for {repo_key}, skipping")
                        continue

                    # Ensure relay data is loaded (creates empty state if needed)
                    self.load_persistent_data(relay_id)

                    # Create minimal folder state if it doesn't exist
                    if folder_id not in self.filemeta_folders[relay_id]:
                        logger.info(
                            f"Creating minimal folder state for {relay_id}/{folder_id} from TOML config"
                        )
                        self.filemeta_folders[relay_id][folder_id] = {}

                        # Save the minimal state
                        self.save_persistent_data(relay_id)

                    # Initialize Git repository
                    self.init_git_repo(relay_id, folder_id)

                    if connector.url:
                        # Configure git remote from TOML
                        success = self.configure_git_remote(
                            relay_id, folder_id, connector.url, connector.remote_name
                        )

                        if success:
                            logger.info(
                                f"Created Git repository from TOML config: {relay_id}/{folder_id} -> {connector.url}"
                            )
                            initialized_count += 1
                        else:
                            logger.warning(
                                f"Created Git repository but failed to configure remote for {relay_id}/{folder_id}"
                            )
                            initialized_count += 1
                    else:
                        logger.info(
                            f"Created local-only Git repository from TOML config: {relay_id}/{folder_id}"
                        )
                        initialized_count += 1

                except Exception as e:
                    logger.error(
                        f"Failed to initialize Git repo from TOML for {connector.relay_id}/{connector.shared_folder_id}: {e}"
                    )

            return initialized_count

        except Exception as e:
            logger.error(f"Error initializing Git repositories from TOML: {e}")
            return 0

    def _setup_global_ssh_environment(self):
        """Set up SSH environment variables globally for all Git operations"""
        if not self.ssh_key_manager:
            return

        # Use the SSH key files created by SSHKeyManager
        private_key_path = self.ssh_key_manager.private_key_path

        # Set up SSH command to use the key file.
        ssh_options = [
            "-o LogLevel=ERROR",
            "-o IdentitiesOnly=yes",
            "-o PasswordAuthentication=no",
            "-o PreferredAuthentications=publickey",
            "-o ConnectTimeout=10",
        ]
        if self.ssh_key_manager.known_hosts:
            ssh_options.extend(
                [
                    "-o StrictHostKeyChecking=yes",
                    f"-o UserKnownHostsFile={self.ssh_key_manager.known_hosts_path}",
                ]
            )
        ssh_options.append(f"-i {private_key_path}")
        ssh_command = "ssh " + " ".join(ssh_options)
        os.environ["GIT_SSH_COMMAND"] = ssh_command

        logger.info(f"Global SSH setup - Using SSH command: {ssh_command}")
        logger.info(f"Global SSH setup - Private key: {private_key_path}")
        if self.ssh_key_manager.known_hosts:
            logger.info(f"Global SSH setup - Known hosts: {self.ssh_key_manager.known_hosts_path}")
        else:
            logger.info("Global SSH setup - Known hosts handled by system defaults or run.sh")

    def _safe_git_operation(self, func, *args, **kwargs):
        """Execute git operation with locking and error recovery"""
        with self.git_lock:
            try:
                return func(*args, **kwargs)
            except git.exc.GitCommandError as e:
                # If git operation fails due to lock files, try cleaning up and retrying once
                if "index.lock" in str(e) or "File exists" in str(e):
                    logger.warning(
                        f"Git operation failed due to lock file, attempting cleanup: {e}"
                    )
                    self._cleanup_git_lock_files()
                    time.sleep(1)  # Brief pause before retry
                    return func(*args, **kwargs)
                else:
                    raise

    def _safe_git_fetch_with_debug(self, origin, repo_key: str):
        """Execute git fetch with enhanced debugging for SSH issues"""
        try:
            # Log SSH environment details
            git_ssh_command = os.environ.get("GIT_SSH_COMMAND")

            logger.info(f"Git fetch debug for {repo_key}:")
            logger.info(f"  GIT_SSH_COMMAND: {git_ssh_command}")
            logger.info(f"  Remote URL: {origin.url}")
            logger.info(f"  SSH key manager available: {self.ssh_key_manager is not None}")

            # Check SSH key files if manager is available
            if self.ssh_key_manager:
                private_key_exists = os.path.exists(self.ssh_key_manager.private_key_path)
                public_key_exists = os.path.exists(self.ssh_key_manager.public_key_path)

                logger.info(
                    f"  Private key file exists: {private_key_exists} ({self.ssh_key_manager.private_key_path})"
                )
                logger.info(
                    f"  Public key file exists: {public_key_exists} ({self.ssh_key_manager.public_key_path})"
                )
                if self.ssh_key_manager.known_hosts:
                    logger.info(f"  Known hosts file: {self.ssh_key_manager.known_hosts_path}")
                else:
                    logger.info("  Known hosts handled by system defaults or run.sh")

            # Perform the fetch
            self._safe_git_operation(lambda: origin.fetch())

        except git.exc.GitCommandError as e:
            logger.error(f"Git fetch failed for {repo_key}: {e}")
            logger.error(f"  Command: {e.command}")
            logger.error(f"  Return code: {e.status}")
            logger.error(f"  Stderr: {e.stderr}")

            # Check if it's an SSH authentication error
            if "Could not read from remote repository" in str(e.stderr):
                logger.error("This appears to be an SSH authentication error.")
                logger.error("Possible causes:")
                logger.error("  1. SSH private key file not properly written from SSH_PRIVATE_KEY")
                logger.error("  2. SSH public key not added to the Git hosting service")
                logger.error("  3. Repository URL is incorrect")
                logger.error("  4. Network connectivity issues")

                # Try to diagnose SSH key issues
                if self.ssh_key_manager:
                    try:
                        public_key = self.ssh_key_manager.get_public_key()
                        logger.info(f"  SSH public key in use: {public_key[:50]}...")
                    except Exception as pk_error:
                        logger.error(f"  Error getting public key: {pk_error}")
                else:
                    logger.error("  SSH key manager not initialized")

            raise

    def get_folder_path_from_folder_resource(self, folder_resource: S3RemoteFolder) -> str:
        """Get the folder path within relay repository using S3RN resource"""
        relay_id = S3RN.get_relay_id(folder_resource)
        folder_uuid = S3RN.get_folder_id(folder_resource)
        return os.path.join(self.get_repo_dir(relay_id), folder_uuid)

    def get_folder_path(self, relay_id: str, folder_uuid: str) -> str:
        """Get the folder path within relay repository"""
        return os.path.join(self.get_repo_dir(relay_id), folder_uuid)

    def get_folder_path_with_prefix(self, relay_id: str, folder_uuid: str) -> str:
        """Get the folder path within relay repository, including any configured prefix"""
        base_path = self.get_folder_path(relay_id, folder_uuid)

        # Check if there's a prefix configured for this folder
        connector = self.git_config.get_connector_for_folder(relay_id, folder_uuid)
        if connector and connector.prefix:
            # Apply the prefix - it will be sanitized when used with _sanitize_path
            return os.path.join(base_path, connector.prefix)

        return base_path

    def load_persistent_data(self, relay_id: str):
        """Load document hashes, subdoc heads, filemeta, and local state."""
        # Ensure relay exists in data structures
        if relay_id not in self.document_hashes:
            self.document_hashes[relay_id] = {}
        if relay_id not in self.subdoc_heads:
            self.subdoc_heads[relay_id] = {}
        if relay_id not in self.filemeta_folders:
            self.filemeta_folders[relay_id] = {}
        if relay_id not in self.local_file_state:
            self.local_file_state[relay_id] = {}
        if relay_id not in self.resource_index:
            self.resource_index[relay_id] = {}

        # Ensure state directory exists
        os.makedirs(self.get_state_dir(relay_id), exist_ok=True)

        # Load document hashes
        hashes_path = self.get_hashes_file_path(relay_id)
        if os.path.exists(hashes_path):
            try:
                with open(hashes_path, "r") as f:
                    self.document_hashes[relay_id] = json.load(f)
            except Exception as e:
                logger.error(f"Error loading document hashes for relay {relay_id}: {e}")
                self.document_hashes[relay_id] = {}

        # Load subdocument index heads
        subdoc_heads_path = self.get_subdoc_heads_file_path(relay_id)
        if os.path.exists(subdoc_heads_path):
            try:
                with open(subdoc_heads_path, "r") as f:
                    self.subdoc_heads[relay_id] = json.load(f)
            except Exception as e:
                logger.error(f"Error loading subdoc heads for relay {relay_id}: {e}")
                self.subdoc_heads[relay_id] = {}

        # Load filemeta
        filemeta_path = self.get_filemeta_file_path(relay_id)
        if os.path.exists(filemeta_path):
            try:
                with open(filemeta_path, "r") as f:
                    self.filemeta_folders[relay_id] = json.load(f)
            except Exception as e:
                logger.error(f"Error loading filemeta for relay {relay_id}: {e}")
                self.filemeta_folders[relay_id] = {}

        # Load local file state
        local_state_path = self.get_local_state_file_path(relay_id)
        if os.path.exists(local_state_path):
            try:
                with open(local_state_path, "r") as f:
                    self.local_file_state[relay_id] = json.load(f)
            except Exception as e:
                logger.error(f"Error loading local state for relay {relay_id}: {e}")
                self.local_file_state[relay_id] = {}

        # Build resource index from loaded data
        with self.resource_index_lock:
            self._build_resource_index(relay_id)

    def _write_json_atomic(self, path: str, data: Any):
        """Write JSON to a temp file and rename it into place so readers and
        crash recovery never observe a partially written state file."""
        temp_path = f"{path}.tmp"
        with open(temp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, path)

    def save_persistent_data(self, relay_id: str):
        """Save document hashes, subdoc heads, filemeta, and local state."""
        # Ensure state directory exists
        os.makedirs(self.get_state_dir(relay_id), exist_ok=True)

        try:
            self._write_json_atomic(
                self.get_hashes_file_path(relay_id), self.document_hashes.get(relay_id, {})
            )
        except Exception as e:
            logger.error(f"Error saving document hashes for relay {relay_id}: {e}")

        try:
            self._write_json_atomic(
                self.get_subdoc_heads_file_path(relay_id),
                self.subdoc_heads.get(relay_id, {}),
            )
        except Exception as e:
            logger.error(f"Error saving subdoc heads for relay {relay_id}: {e}")

        try:
            self._write_json_atomic(
                self.get_filemeta_file_path(relay_id), self.filemeta_folders.get(relay_id, {})
            )
        except Exception as e:
            logger.error(f"Error saving filemeta for relay {relay_id}: {e}")

        try:
            self._write_json_atomic(
                self.get_local_state_file_path(relay_id), self.local_file_state.get(relay_id, {})
            )
        except Exception as e:
            logger.error(f"Error saving local state for relay {relay_id}: {e}")

        # Rebuild resource index after saving data
        with self.resource_index_lock:
            self._build_resource_index(relay_id)

    def init_git_repo(self, relay_id: str, folder_id: str) -> git.Repo:
        """Initialize git repository for a specific folder within a relay"""
        folder_path = self.get_folder_path(relay_id, folder_id)
        os.makedirs(folder_path, exist_ok=True)

        repo_key = f"{relay_id}/{folder_id}"

        try:
            # Try to open existing repo
            self.git_repos[repo_key] = git.Repo(folder_path)
            print(
                f"Using existing git repository for folder {folder_id} in relay {relay_id} at {folder_path}"
            )

            # If repo exists and has remotes, pull latest changes
            git_repo = self.git_repos[repo_key]
            self._ensure_configured_branch(repo_key, git_repo)
            if git_repo.remotes:
                self._pull_from_remote(repo_key, git_repo)

        except git.InvalidGitRepositoryError:
            # Repository doesn't exist locally, check if we should clone from remote
            connector = self.git_config.get_connector_for_folder(relay_id, folder_id)
            if connector and connector.url:
                # Clone from remote
                try:
                    print(f"Cloning repository from {connector.url} for folder {folder_id}")
                    self.git_repos[repo_key] = git.Repo.clone_from(
                        connector.url, folder_path, branch=connector.branch
                    )
                    self._repair_tracking_branch_if_obvious(repo_key, self.git_repos[repo_key])
                    print(f"Successfully cloned repository to {folder_path}")
                    return self.git_repos[repo_key]
                except Exception as e:
                    logger.warning(f"Failed to clone repository {connector.url}: {e}")
                    # Fall through to create new repo

            # Create new repo
            self.git_repos[repo_key] = git.Repo.init(folder_path, initial_branch="main")
            print(
                f"Initialized new git repository for folder {folder_id} in relay {relay_id} at {folder_path} with main branch"
            )

            # Create initial commit
            try:
                # Try to get HEAD - if it fails, repo has no commits
                self.git_repos[repo_key].head.commit
                has_commits = True
            except ValueError:
                # No HEAD reference means no commits
                has_commits = False

            if not has_commits:
                # Add .gitignore for content only
                gitignore_path = os.path.join(folder_path, ".gitignore")
                with open(gitignore_path, "w") as f:
                    f.write("# Relay sync repository - content only\n")

                self.git_repos[repo_key].index.add([".gitignore"])
                self.git_repos[repo_key].index.commit("Initial commit")
                print(f"Created initial git commit for folder {folder_id} in relay {relay_id}")

        return self.git_repos[repo_key]

    def _ensure_configured_branch(self, repo_key: str, git_repo: git.Repo):
        """Reattach a detached HEAD without changing the index or working tree."""
        if not git_repo.head.is_detached:
            return git_repo.active_branch

        try:
            relay_id, folder_id = repo_key.split("/", 1)
        except ValueError:
            relay_id = folder_id = ""

        connector = self.git_config.get_connector_for_folder(relay_id, folder_id)
        if connector:
            branch_name = connector.branch
        elif len(git_repo.heads) == 1:
            branch_name = git_repo.heads[0].name
        else:
            branch_name = "main"

        detached_commit = git_repo.head.commit
        target_branch = next(
            (branch for branch in git_repo.heads if branch.name == branch_name),
            None,
        )
        if target_branch is None:
            target_branch = git_repo.create_head(branch_name, detached_commit)
        elif target_branch.commit != detached_commit:
            # Preserve the old branch tip before moving the configured branch
            # to the detached worktree's commit. This keeps both histories
            # recoverable while allowing normal pull/push recovery to resume.
            recovery_name = (
                f"recovery/{branch_name.replace('/', '-')}-{target_branch.commit.hexsha[:12]}"
            )
            if recovery_name not in [branch.name for branch in git_repo.heads]:
                git_repo.create_head(recovery_name, target_branch.commit)
            target_branch.set_commit(detached_commit)

        git_repo.head.reference = target_branch
        logger.warning(
            f"Reattached detached HEAD for {repo_key} to {branch_name} "
            f"at {detached_commit.hexsha[:12]}"
        )
        return target_branch

    def configure_git_remote(
        self, relay_id: str, folder_id: str, remote_url: str, remote_name: str = "origin"
    ):
        """Configure git remote for a folder repository"""
        try:
            repo_key = f"{relay_id}/{folder_id}"
            git_repo = self.git_repos.get(repo_key)
            if not git_repo:
                logger.error(f"No git repository found for folder {folder_id} in relay {relay_id}")
                return False

            self._ensure_configured_branch(repo_key, git_repo)

            # Check if remote already exists
            if remote_name in [r.name for r in git_repo.remotes]:
                # Update existing remote URL
                remote = git_repo.remotes[remote_name]
                remote.set_url(remote_url)
                print(
                    f"Updated remote '{remote_name}' URL for folder {folder_id} in relay {relay_id}: {remote_url}"
                )
            else:
                # Add new remote
                remote = git_repo.create_remote(remote_name, remote_url)
                print(
                    f"Added remote '{remote_name}' for folder {folder_id} in relay {relay_id}: {remote_url}"
                )

            self._repair_tracking_branch_if_obvious(repo_key, git_repo, remote)

            return True

        except Exception as e:
            logger.error(
                f"Error configuring remote for folder {folder_id} in relay {relay_id}: {e}"
            )
            return False

    def _auto_configure_git_remote(self, relay_id: str, folder_id: str):
        """Automatically configure git remote based on TOML configuration"""
        try:
            connector = self.git_config.get_connector_for_folder(relay_id, folder_id)
            if connector and connector.url:
                success = self.configure_git_remote(
                    relay_id, folder_id, connector.url, connector.remote_name
                )
                if success:
                    logger.info(
                        f"Auto-configured git remote for folder {folder_id} in relay {relay_id}: "
                        f"{connector.remote_name} -> {connector.url}"
                    )
                else:
                    logger.warning(
                        f"Failed to auto-configure git remote for folder {folder_id} in relay {relay_id}"
                    )
            else:
                if connector:
                    logger.info(
                        f"Git connector for folder {folder_id} in relay {relay_id} is local-only"
                    )
                else:
                    logger.debug(
                        f"No git connector configured for folder {folder_id} in relay {relay_id}"
                    )
        except Exception as e:
            logger.error(f"Error auto-configuring git remote for folder {folder_id}: {e}")

    def push_to_remote(self, relay_id: str, folder_id: str) -> bool:
        """Manually push a specific folder repository to its remote"""
        try:
            repo_key = f"{relay_id}/{folder_id}"
            git_repo = self.git_repos.get(repo_key)
            if not git_repo:
                logger.error(f"No git repository found for folder {folder_id} in relay {relay_id}")
                return False

            self._push_to_remote(repo_key, git_repo)
            return True

        except Exception as e:
            logger.error(f"Error pushing folder {folder_id} in relay {relay_id}: {e}")
            return False

    def push_all_repos(self) -> int:
        """Push all folder repositories that have remotes configured

        Returns:
            int: Number of repositories successfully pushed
        """
        pushed_count = 0
        for repo_key, git_repo in self.git_repos.items():
            if not self._is_repo_key_configured(repo_key):
                logger.info(f"Skipping push for unconfigured repository {repo_key}")
                continue

            if git_repo.remotes:
                try:
                    self._push_to_remote(repo_key, git_repo)
                    pushed_count += 1
                except Exception as e:
                    logger.error(f"Failed to push repository {repo_key}: {e}")

        return pushed_count

    def commit_changes(self) -> bool:
        """Commit changes to git repositories if there are any, and push to remote if configured

        Returns:
            bool: True if any commits were made, False otherwise
        """
        try:
            committed_any = False
            # Check each folder repository for changes
            for repo_key, git_repo in self.git_repos.items():
                if not self._is_repo_key_configured(repo_key):
                    logger.info(f"Skipping commit for unconfigured repository {repo_key}")
                    continue

                self._ensure_configured_branch(repo_key, git_repo)

                if git_repo.is_dirty() or git_repo.untracked_files:
                    # Pull latest changes before committing if remote is configured
                    if git_repo.remotes:
                        self._pull_from_remote(repo_key, git_repo)

                    # Add all changes using safe git operation
                    self._safe_git_operation(lambda: git_repo.git.add(A=True))

                    # Create commit message
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    commit_msg = f"Auto-sync: {timestamp}"

                    # Commit changes using safe git operation
                    self._safe_git_operation(lambda: git_repo.index.commit(commit_msg))
                    print(f"Git commit for repository {repo_key}: {commit_msg}")
                    committed_any = True

                    # Push to remote if configured
                    self._push_to_remote(repo_key, git_repo)

                elif git_repo.remotes and self._has_unpushed_commits(repo_key, git_repo):
                    logger.info(f"Pushing previously unpushed commits for repository {repo_key}")
                    self._push_to_remote(repo_key, git_repo)

            return committed_any

        except Exception as e:
            logger.error(f"Error committing to git: {e}")
            logger.error(f"Git commit traceback: {traceback.format_exc()}")
            return False

    def _has_unpushed_commits(self, repo_key: str, git_repo: git.Repo) -> bool:
        self._ensure_configured_branch(repo_key, git_repo)
        try:
            current_branch = git_repo.active_branch
        except (TypeError, ValueError):
            return False

        tracking_branch = current_branch.tracking_branch()
        if tracking_branch is None:
            # This runs on every commit tick; without an upstream it needs a network
            # fetch, so rate-limit the fetch to avoid hammering the remote when the
            # tracking branch cannot be repaired (e.g. remote branch does not exist).
            now = time.monotonic()
            last_fetch = self._unpushed_check_fetch_times.get(repo_key, 0.0)
            if now - last_fetch < self.UNPUSHED_CHECK_FETCH_INTERVAL_SECONDS:
                return False
            self._unpushed_check_fetch_times[repo_key] = now

            remote = (
                git_repo.remotes.origin
                if "origin" in [r.name for r in git_repo.remotes]
                else git_repo.remotes[0]
            )
            self._safe_git_fetch_with_debug(remote, repo_key)
            tracking_branch = self._repair_tracking_branch_if_obvious(repo_key, git_repo, remote)

        if tracking_branch is None:
            remote_name = (
                "origin"
                if "origin" in [r.name for r in git_repo.remotes]
                else git_repo.remotes[0].name
            )
            remote_branch = f"{remote_name}/{current_branch.name}"
            try:
                remote_commit = git_repo.commit(remote_branch)
            except Exception:
                return True
            return remote_commit.hexsha != current_branch.commit.hexsha

        try:
            ahead = git_repo.git.rev_list(
                "--count", f"{tracking_branch.name}..{current_branch.name}"
            )
            return int(ahead) > 0
        except Exception as e:
            logger.debug(f"Could not determine unpushed commit state: {e}")
            return False

    def _repair_tracking_branch_if_obvious(self, repo_key: str, git_repo: git.Repo, remote=None):
        """Set branch upstream when local branch and remote branch clearly match."""
        try:
            current_branch = git_repo.active_branch
        except (TypeError, ValueError):
            return None

        tracking_branch = current_branch.tracking_branch()
        if tracking_branch is not None:
            return tracking_branch

        if not git_repo.remotes:
            return None

        if remote is None:
            remote = (
                git_repo.remotes.origin
                if "origin" in [r.name for r in git_repo.remotes]
                else git_repo.remotes[0]
            )

        remote_branch_name = f"{remote.name}/{current_branch.name}"
        try:
            git_repo.commit(remote_branch_name)
        except Exception:
            logger.debug(
                f"No obvious upstream to repair for {repo_key}: missing {remote_branch_name}"
            )
            return None

        try:
            self._safe_git_operation(
                lambda: git_repo.git.branch(
                    "--set-upstream-to", remote_branch_name, current_branch.name
                )
            )
            logger.info(
                f"Repaired upstream tracking for {repo_key}: "
                f"{current_branch.name} -> {remote_branch_name}"
            )
            return current_branch.tracking_branch()
        except git.exc.GitCommandError as e:
            logger.warning(f"Failed to repair upstream tracking for {repo_key}: {e}")
            return None

    def _abort_stuck_rebase(self, git_repo: git.Repo, repo_key: str):
        """Abort any in-progress rebase so subsequent git operations don't wedge."""
        rebase_merge_dir = os.path.join(git_repo.git_dir, "rebase-merge")
        rebase_apply_dir = os.path.join(git_repo.git_dir, "rebase-apply")
        if os.path.isdir(rebase_merge_dir) or os.path.isdir(rebase_apply_dir):
            logger.warning(f"Aborting stuck rebase for {repo_key}")
            try:
                git_repo.git.rebase("--abort")
            except git.exc.GitCommandError as e:
                logger.warning(
                    f"rebase --abort failed for {repo_key}, removing rebase state manually: {e}"
                )
                shutil.rmtree(rebase_merge_dir, ignore_errors=True)
                shutil.rmtree(rebase_apply_dir, ignore_errors=True)

    def _pull_from_remote(self, repo_key: str, git_repo: git.Repo):
        """Pull latest changes from remote repository using rebase"""
        try:
            self._ensure_configured_branch(repo_key, git_repo)

            # Check if any remotes are configured
            if not git_repo.remotes:
                logger.debug(f"No remotes configured for repository {repo_key}, skipping pull")
                return

            self._abort_stuck_rebase(git_repo, repo_key)

            # Get the default remote (usually 'origin')
            origin = (
                git_repo.remotes.origin
                if "origin" in [r.name for r in git_repo.remotes]
                else git_repo.remotes[0]
            )

            # Check if we have any local commits that need to be preserved
            try:
                current_branch = git_repo.active_branch
                tracking_branch = current_branch.tracking_branch()

                if tracking_branch is None:
                    # No tracking branch set up yet. Fetch, then repair if the
                    # matching remote branch exists.
                    print(f"Fetching from {origin.name} for repository {repo_key}")
                    self._safe_git_fetch_with_debug(origin, repo_key)
                    tracking_branch = self._repair_tracking_branch_if_obvious(
                        repo_key, git_repo, origin
                    )
                    if tracking_branch is None:
                        return

                # The remote-tracking ref only moves when this clone fetches or pushes,
                # so it is stale whenever anything else (another connector, a human)
                # pushed last. Refresh it or the ahead/behind comparison below is
                # against a snapshot and we commit on a stale parent.
                self._safe_git_operation(lambda: origin.fetch())

                # Check if local branch is ahead of remote
                ahead_behind = git_repo.git.rev_list(
                    "--left-right", "--count", f"{tracking_branch.name}...{current_branch.name}"
                )
                behind, ahead = ahead_behind.split("\t")

                if int(behind) > 0:
                    if int(ahead) > 0:
                        # Both ahead and behind - need to merge remote changes
                        print(
                            f"Pulling from {origin.name} for repository {repo_key} with conflict resolution"
                        )
                        try:
                            # Try regular rebase first
                            self._safe_git_operation(
                                lambda: git_repo.git.pull(
                                    "--rebase", origin.name, current_branch.name
                                )
                            )
                            print(f"Successfully rebased {repo_key}")
                        except git.exc.GitCommandError as rebase_error:
                            if (
                                "CONFLICT" in str(rebase_error)
                                or "merge conflict" in str(rebase_error).lower()
                            ):
                                logger.info(
                                    f"Merge conflicts detected for {repo_key}, resolving conflicts in our favor for our files..."
                                )

                                # Resolve conflicts by checking each conflicted file
                                self._resolve_conflicts_in_our_favor(git_repo, repo_key)

                                # Continue the rebase
                                try:
                                    git_repo.git.rebase("--continue")
                                    print(f"Resolved conflicts and continued rebase for {repo_key}")
                                except git.exc.GitCommandError:
                                    # If rebase --continue fails, abort and try merge instead
                                    logger.info(
                                        f"Rebase failed, trying merge strategy for {repo_key}"
                                    )
                                    git_repo.git.rebase("--abort")

                                    # Try merge approach
                                    self._safe_git_operation(
                                        lambda: git_repo.git.pull(origin.name, current_branch.name)
                                    )

                                    # Resolve any merge conflicts
                                    if git_repo.git.diff("--name-only", "--diff-filter=U"):
                                        self._resolve_conflicts_in_our_favor(git_repo, repo_key)
                                        git_repo.git.commit("--no-edit")

                                    print(
                                        f"Merged remote changes with conflicts resolved for {repo_key}"
                                    )
                            else:
                                self._abort_stuck_rebase(git_repo, repo_key)
                                raise rebase_error
                    else:
                        # Only behind - fast-forward pull
                        print(f"Fast-forward pull from {origin.name} for repository {repo_key}")
                        self._safe_git_operation(
                            lambda: git_repo.git.pull(origin.name, current_branch.name)
                        )
                else:
                    # Up to date or only ahead
                    print(f"Repository {repo_key} is up to date with remote")

            except Exception as e:
                logger.warning(f"Error checking branch status for {repo_key}: {e}")
                # Fallback to simple fetch
                print(f"Fetching from {origin.name} for repository {repo_key}")
                self._safe_git_fetch_with_debug(origin, repo_key)

        except Exception as e:
            logger.error(f"Error pulling from remote for repository {repo_key}: {e}")
            logger.error(f"Pull traceback: {traceback.format_exc()}")

    def _resolve_conflicts_in_our_favor(self, git_repo: git.Repo, repo_key: str):
        """Resolve merge conflicts by keeping our version for files within our managed prefix"""
        try:
            # Get list of conflicted files
            conflicted_files = git_repo.git.diff("--name-only", "--diff-filter=U").splitlines()

            if not conflicted_files:
                return

            logger.info(f"Resolving {len(conflicted_files)} conflicted files for {repo_key}")

            # Parse repo_key to get relay_id and folder_id
            relay_id, folder_id = repo_key.split("/", 1)

            # Get our managed prefix if any
            connector = self.git_config.get_connector_for_folder(relay_id, folder_id)
            our_prefix = connector.prefix if connector else ""

            resolved_count = 0
            skipped_count = 0

            for file_path in conflicted_files:
                # Check if this file is within our managed prefix
                is_our_file = False

                if our_prefix:
                    # If we have a prefix, only resolve conflicts for files within it
                    is_our_file = file_path.startswith(our_prefix + "/") or file_path == our_prefix
                else:
                    # If no prefix, we manage all files (but be more conservative)
                    # Only auto-resolve files that look like content files
                    is_our_file = (
                        file_path.endswith((".md", ".txt", ".canvas", ".json"))
                        or not file_path.startswith(".")
                        or file_path  # Skip dotfiles
                        == ".gitignore"  # Except .gitignore which we manage
                    )

                if is_our_file:
                    # Resolve in our favor - use our version
                    try:
                        git_repo.git.checkout("--ours", file_path)
                        git_repo.git.add(file_path)
                        resolved_count += 1
                        logger.debug(f"Resolved conflict for {file_path} in favor of our version")
                    except Exception as e:
                        logger.warning(f"Failed to resolve conflict for {file_path}: {e}")
                else:
                    # Not our file - use their version to preserve external changes
                    try:
                        git_repo.git.checkout("--theirs", file_path)
                        git_repo.git.add(file_path)
                        skipped_count += 1
                        logger.debug(
                            f"Resolved conflict for {file_path} in favor of their version (external file)"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to resolve conflict for {file_path}: {e}")

            logger.info(
                f"Conflict resolution for {repo_key}: {resolved_count} files kept ours, {skipped_count} files kept theirs"
            )

        except Exception as e:
            logger.error(f"Error during conflict resolution for {repo_key}: {e}")
            logger.error(f"Conflict resolution traceback: {traceback.format_exc()}")

    def _push_and_verify(self, origin, *args, **kwargs):
        """Push and raise GitCommandError if any ref was rejected.

        Remote.push() does not raise on rejected refs: when porcelain output was
        parsed, the nonzero exit is swallowed and the rejection is only visible in
        the returned PushInfo flags. Raising here lets callers route rejections
        into the existing pull-and-retry recovery path.
        """
        push_infos = self._safe_git_operation(lambda: origin.push(*args, **kwargs))
        failed = [
            info
            for info in push_infos
            if info.flags
            & (info.ERROR | info.REJECTED | info.REMOTE_REJECTED | info.REMOTE_FAILURE)
        ]
        if failed:
            raise git.exc.GitCommandError(
                ["git", "push"],
                1,
                stderr="push rejected: " + "; ".join(info.summary.strip() for info in failed),
            )

    def _push_to_remote(self, repo_key: str, git_repo: git.Repo):
        """Push commits to remote repository if configured"""
        try:
            self._ensure_configured_branch(repo_key, git_repo)

            # Check if any remotes are configured
            if not git_repo.remotes:
                logger.debug(f"No remotes configured for repository {repo_key}, skipping push")
                return

            # Get the default remote (usually 'origin')
            origin = (
                git_repo.remotes.origin
                if "origin" in [r.name for r in git_repo.remotes]
                else git_repo.remotes[0]
            )

            # Check if current branch has an upstream
            try:
                current_branch = git_repo.active_branch
                if current_branch.tracking_branch() is None:
                    self._safe_git_fetch_with_debug(origin, repo_key)
                    tracking_branch = self._repair_tracking_branch_if_obvious(
                        repo_key, git_repo, origin
                    )
                else:
                    tracking_branch = current_branch.tracking_branch()

                if tracking_branch is None:
                    # Set upstream for first push
                    self._push_and_verify(origin, current_branch.name, set_upstream=True)
                    print(
                        f"Git push (set upstream) for repository {repo_key} to {origin.name}/{current_branch.name}"
                    )
                else:
                    # Regular push - don't force to avoid clobbering other commits
                    try:
                        self._push_and_verify(origin)
                        print(
                            f"Git push for repository {repo_key} to {origin.name}/{current_branch.name}"
                        )
                    except git.exc.GitCommandError as push_error:
                        if "non-fast-forward" in str(push_error) or "rejected" in str(push_error):
                            # Push rejected - this means there are remote commits we haven't seen
                            # Pull first to merge remote changes, then push again
                            logger.info(
                                f"Push rejected for {repo_key}, pulling remote changes first"
                            )
                            self._pull_from_remote(repo_key, git_repo)

                            # Try pushing again after pull
                            try:
                                self._push_and_verify(origin)
                                print(f"Git push successful after pull for repository {repo_key}")
                            except git.exc.GitCommandError:
                                # If still failing, there might be an issue - log but don't force
                                logger.warning(
                                    f"Push still failing for {repo_key} after pull, skipping"
                                )
                        else:
                            raise push_error

            except git.exc.GitCommandError as e:
                # Handle common push errors gracefully
                if "non-fast-forward" in str(e):
                    logger.warning(
                        f"Push rejected for repository {repo_key}: non-fast-forward. Manual intervention may be needed."
                    )
                elif "Permission denied" in str(e) or "Authentication failed" in str(e):
                    logger.warning(
                        f"Push failed for repository {repo_key}: authentication error. Check SSH keys or credentials."
                    )
                else:
                    logger.error(f"Push failed for repository {repo_key}: {e}")

        except Exception as e:
            logger.error(f"Error pushing to remote for repository {repo_key}: {e}")
            logger.error(f"Push traceback: {traceback.format_exc()}")

    def get_git_repo(self, relay_id: str, folder_id: str) -> Optional[git.Repo]:
        """Get git repository for a folder within a relay"""
        repo_key = f"{relay_id}/{folder_id}"
        return self.git_repos.get(repo_key)

    def _sanitize_path(self, path: str, base_directory: str) -> str:
        """
        Sanitize a file path to prevent directory traversal attacks.

        Args:
            path: The input path to sanitize
            base_directory: The base directory that the path must stay within

        Returns:
            str: A safe path guaranteed to be within base_directory

        Raises:
            ValueError: If the path attempts to escape the base directory
        """
        if not path:
            raise ValueError("Empty path provided")

        # Remove leading slashes
        clean_path = path.lstrip("/")

        # Reject paths containing .. path components (but allow ".." in filenames)
        if any(part == ".." for part in clean_path.split("/")):
            raise ValueError(f"Path '{path}' contains directory traversal sequences")

        # Build the full path
        full_path = os.path.join(base_directory, clean_path)

        # Normalize and make absolute
        normalized_path = os.path.abspath(full_path)
        base_absolute = os.path.abspath(base_directory)

        # Ensure the path is within the base directory but not the base directory itself
        if normalized_path == base_absolute:
            raise ValueError(f"Path '{path}' attempts to escape base directory")
        if not normalized_path.startswith(base_absolute + os.sep):
            raise ValueError(f"Path '{path}' attempts to escape base directory")

        return normalized_path

    def ensure_parent_directory(self, full_path: str):
        """Create parent directories if they don't exist"""
        parent_dir = os.path.dirname(full_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

    def update_local_file_state(
        self, document_resource: ResourceInterface, path: str, file_hash: Optional[str] = None
    ):
        """Update local file state tracking using S3RN resource"""
        # Extract IDs from S3RN resource
        relay_id = S3RN.get_relay_id(document_resource)
        folder_uuid = S3RN.get_folder_id(document_resource)
        doc_id = document_resource.get_resource_id()

        # Use folder_uuid for state tracking
        folder_uuid = S3RN.get_folder_id(document_resource)

        if relay_id not in self.local_file_state:
            self.local_file_state[relay_id] = {}
        if folder_uuid not in self.local_file_state[relay_id]:
            self.local_file_state[relay_id][folder_uuid] = {}

        # Determine type from S3RN resource using instance method
        resource_type = document_resource.get_resource_type()

        self.local_file_state[relay_id][folder_uuid][path] = {
            "doc_id": doc_id,
            "hash": file_hash,
            "type": resource_type,
            "modified": time.time(),
        }

    def remove_local_file_state(self, relay_id: str, folder_id: str, path: str):
        """Remove local file state tracking"""
        if relay_id in self.local_file_state:
            if folder_id in self.local_file_state[relay_id]:
                self.local_file_state[relay_id][folder_id].pop(path, None)

    def find_local_file_by_doc_id(
        self, relay_id: str, folder_id: str, doc_id: str
    ) -> Optional[str]:
        """Find local file path by document ID within a specific folder"""
        relay_state = self.local_file_state.get(relay_id, {})
        folder_state = relay_state.get(folder_id, {})
        for path, state in folder_state.items():
            if state.get("doc_id") == doc_id:
                return path
        return None

    def write_file_content(
        self, document_resource: S3RNType, path: str, content: str, file_hash: Optional[str] = None
    ):
        """Write content to a file and update local state"""
        # Extract IDs from S3RN document resource
        relay_id = S3RN.get_relay_id(document_resource)
        folder_uuid = S3RN.get_folder_id(document_resource)

        # Build full path within folder subdirectory, including any configured prefix
        folder_path = self.get_folder_path_with_prefix(relay_id, folder_uuid)
        full_path = self._sanitize_path(path, folder_path)

        # Create parent directories if needed
        self.ensure_parent_directory(full_path)

        # Check if the target path is a directory
        if os.path.exists(full_path) and os.path.isdir(full_path):
            logger.warning(f"Cannot write file to {full_path}: path exists as directory")
            raise ValueError(f"Cannot write file: {full_path} exists as directory")

        # Write content to file
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content or "")

        # Update local state using S3RN resource
        self.update_local_file_state(document_resource, path, file_hash)

        return full_path

    def write_binary_file_content(
        self,
        document_resource: S3RNType,
        path: str,
        content: bytes,
        file_hash: Optional[str] = None,
    ):
        """Write binary content to a file and update local state"""
        # Extract IDs from S3RN document resource
        relay_id = S3RN.get_relay_id(document_resource)
        folder_uuid = S3RN.get_folder_id(document_resource)

        # Build full path within folder subdirectory, including any configured prefix
        folder_path = self.get_folder_path_with_prefix(relay_id, folder_uuid)
        full_path = self._sanitize_path(path, folder_path)

        # Create parent directories if needed
        self.ensure_parent_directory(full_path)

        # Check if the target path is a directory
        if os.path.exists(full_path) and os.path.isdir(full_path):
            logger.warning(f"Cannot write file to {full_path}: path exists as directory")
            raise ValueError(f"Cannot write file: {full_path} exists as directory")

        # Write binary content to file
        with open(full_path, "wb") as f:
            f.write(content or b"")

        # Update local state using S3RN resource
        self.update_local_file_state(document_resource, path, file_hash)

        return full_path

    def create_directory(self, folder_resource: S3RemoteFolder, path: str) -> str:
        """Create directory structure for folder-type items"""
        # Extract IDs from S3RN folder resource
        relay_id = S3RN.get_relay_id(folder_resource)
        folder_uuid = S3RN.get_folder_id(folder_resource)

        # Build full path within folder subdirectory
        folder_path = self.get_folder_path_with_prefix(relay_id, folder_uuid)
        full_path = self._sanitize_path(path, folder_path)

        # Create directory structure
        os.makedirs(full_path, exist_ok=True)

        return full_path

    def move_file(
        self, document_resource: S3RNType, from_path: str, to_path: str
    ) -> tuple[str, str]:
        """Move a file from one path to another and update local state"""
        # Extract IDs from S3RN document resource
        relay_id = S3RN.get_relay_id(document_resource)
        folder_uuid = S3RN.get_folder_id(document_resource)

        # Build full paths within folder subdirectory, including any configured prefix
        folder_path = self.get_folder_path_with_prefix(relay_id, folder_uuid)
        old_full_path = self._sanitize_path(from_path, folder_path)
        new_full_path = self._sanitize_path(to_path, folder_path)

        if os.path.exists(old_full_path):
            # Create parent directories if needed
            self.ensure_parent_directory(new_full_path)

            # Move the file
            shutil.move(old_full_path, new_full_path)

            # Update local state using folder_uuid for state tracking
            if relay_id in self.local_file_state and folder_uuid in self.local_file_state[relay_id]:
                folder_state = self.local_file_state[relay_id][folder_uuid]
                old_state = folder_state.pop(from_path, {})
                folder_state[to_path] = old_state
                folder_state[to_path]["modified"] = time.time()

        return old_full_path, new_full_path

    def delete_file(self, folder_resource: S3RemoteFolder, path: str) -> str:
        """Delete a file and update local state"""
        # Extract IDs from S3RN resource
        relay_id = S3RN.get_relay_id(folder_resource)
        folder_uuid = S3RN.get_folder_id(folder_resource)

        # Build full path within folder subdirectory, including any configured prefix
        folder_path = self.get_folder_path_with_prefix(relay_id, folder_uuid)
        full_path = self._sanitize_path(path, folder_path)

        if os.path.exists(full_path):
            os.remove(full_path)

            # Remove from local state using folder_uuid for state tracking
            self.remove_local_file_state(relay_id, folder_uuid, path)

        return full_path

    def _build_resource_index(self, relay_id: str):
        """Build in-memory resource index from existing data sources"""
        with self.resource_index_lock:
            if relay_id not in self.resource_index:
                self.resource_index[relay_id] = {}

            relay_index = self.resource_index[relay_id]
            relay_index.clear()  # Rebuild from scratch
            restrict_to_configured_folders = bool(self.git_config.connectors)

            # Index folders from filemeta_folders
            for folder_id in self.filemeta_folders.get(relay_id, {}).keys():
                if not self.should_sync_folder(relay_id, folder_id):
                    continue

                relay_index[folder_id] = {
                    "type": "folder",
                    "folder_id": folder_id,
                    "path": None,  # Folders don't have paths, they contain files
                    "metadata": {},
                }

            # Index documents from local_file_state (the authoritative source)
            for folder_id, folder_state in self.local_file_state.get(relay_id, {}).items():
                if not self.should_sync_folder(relay_id, folder_id):
                    continue

                for path, file_info in folder_state.items():
                    if isinstance(file_info, dict) and "doc_id" in file_info:
                        resource_id = file_info["doc_id"]

                        # Skip compound IDs - they're legacy and should be ignored
                        if "-" in resource_id and len(resource_id.split("-")) > 5:
                            logger.debug(f"Skipping legacy compound ID: {resource_id}")
                            continue

                        # Use stored type, with fallback to extension-based inference for legacy data
                        resource_type = file_info.get("type")
                        if not resource_type:
                            # Fallback for legacy local_file_state entries without type
                            if path.lower().endswith(".canvas"):
                                resource_type = "canvas"
                            elif path.lower().endswith((".md", ".txt", ".json")):
                                resource_type = "document"
                            else:
                                resource_type = "file"

                        relay_index[resource_id] = {
                            "type": resource_type,
                            "folder_id": folder_id,
                            "path": path,
                            "metadata": {"id": resource_id, "type": resource_type},
                        }

            # Index documents from filemeta_folders (includes documents not yet synced to disk)
            for folder_id, filemeta in self.filemeta_folders.get(relay_id, {}).items():
                if not self.should_sync_folder(relay_id, folder_id):
                    continue

                for path, metadata in filemeta.items():
                    if isinstance(metadata, dict) and "id" in metadata:
                        resource_id = metadata["id"]

                        # Skip compound IDs - they're legacy and should be ignored
                        if "-" in resource_id and len(resource_id.split("-")) > 5:
                            logger.debug(f"Skipping legacy compound ID in filemeta: {resource_id}")
                            continue

                        # Skip folders - they're already indexed above
                        if metadata.get("type") == "folder":
                            continue

                        # Only add if not already in index (local_file_state takes precedence)
                        if resource_id not in relay_index:
                            resource_type = metadata.get("type", "document")
                            relay_index[resource_id] = {
                                "type": resource_type,
                                "folder_id": folder_id,
                                "path": path,
                                "metadata": metadata,
                            }

            # Index documents from document_hashes (may include standalone documents)
            if restrict_to_configured_folders:
                return

            for doc_id in self.document_hashes.get(relay_id, {}).keys():
                # Skip compound IDs - they're legacy and should be ignored
                if "-" in doc_id and len(doc_id.split("-")) > 5:
                    logger.debug(f"Skipping legacy compound ID in document_hashes: {doc_id}")
                    continue

                if doc_id not in relay_index:
                    # This is a standalone document not in any folder
                    relay_index[doc_id] = {
                        "type": "standalone_document",
                        "folder_id": None,
                        "path": None,
                        "metadata": {},
                    }

    def lookup_resource(self, relay_id: str, resource_id: str) -> Optional[S3RNType]:
        """Look up resource by resource_id and return S3RN object"""
        with self.resource_index_lock:
            if relay_id not in self.resource_index:
                return None

            resource_info = self.resource_index[relay_id].get(resource_id)
            if not resource_info:
                return None

            return self._create_s3rn_from_index(relay_id, resource_id, resource_info)

    def get_resource_path(self, relay_id: str, resource_id: str) -> Optional[str]:
        """Get the path of a resource within its folder (since this isn't directly available from S3RN)"""
        with self.resource_index_lock:
            if relay_id not in self.resource_index:
                return None
            resource_info = self.resource_index[relay_id].get(resource_id)
            if resource_info:
                return resource_info.get("path")
            return None

    def update_resource_index_for_document(
        self, relay_id: str, resource_id: str, folder_id: str, path: str, metadata: Dict
    ):
        """Update resource index when a document is added/updated"""
        with self.resource_index_lock:
            if relay_id not in self.resource_index:
                self.resource_index[relay_id] = {}

            # Use metadata type directly, no extension-based inference
            resource_type = metadata.get("type", "unknown")

            self.resource_index[relay_id][resource_id] = {
                "type": resource_type,
                "folder_id": folder_id,
                "path": path,
                "metadata": metadata,
            }

    def remove_resource_from_index(self, relay_id: str, resource_id: str):
        """Remove a resource from the index when it's deleted"""
        with self.resource_index_lock:
            if relay_id in self.resource_index:
                self.resource_index[relay_id].pop(resource_id, None)

    def _create_s3rn_from_index(
        self, relay_id: str, resource_id: str, resource_info: Dict
    ) -> Optional[S3RNType]:
        """Create S3RN object from resource index information"""

        resource_type = resource_info.get("type", "unknown")
        folder_id = resource_info.get("folder_id")

        # Handle special case for standalone documents
        if resource_type == "standalone_document":
            logger.warning(
                f"Standalone document {resource_id} cannot be represented as proper S3RN resource"
            )
            return None

        # Map to S3RN resource category
        s3rn_category = get_s3rn_resource_category(resource_type)

        if s3rn_category == "folder":
            return S3RemoteFolder(relay_id, resource_id)
        elif s3rn_category == "document":
            if folder_id:
                return S3RemoteDocument(relay_id, folder_id, resource_id)
        elif s3rn_category == "canvas":
            if folder_id:
                return S3RemoteCanvas(relay_id, folder_id, resource_id)
        elif s3rn_category == "file":
            if folder_id:
                return S3RemoteFile(relay_id, folder_id, resource_id)

        # If we can't determine the type or missing required data, return None
        logger.warning(
            f"Cannot create S3RN resource for type '{resource_type}' (category '{s3rn_category}') with folder_id '{folder_id}' - missing folder or unsupported type"
        )
        return None
