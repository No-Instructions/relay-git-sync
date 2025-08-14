#!/usr/bin/env python3

import json
import os
import time
import shutil
import logging
import traceback
import signal
import atexit
import threading
import glob
from typing import Dict, Any, Optional, List
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import git
from s3rn import (
    S3RNType,
    S3RN,
    S3RemoteFolder,
    S3RemoteDocument,
    S3RemoteCanvas,
    S3RemoteFile,
    ResourceInterface,
)
from models import get_s3rn_resource_category
from git_config import GitConnectorConfig

logger = logging.getLogger(__name__)


class SSHKeyManager:
    """Manage SSH keys for git authentication using environment variables"""

    def __init__(self):
        self._temp_key_path = None
        self._setup_temp_key()

    def _setup_temp_key(self):
        """Create temporary key file from environment variable"""
        private_key_pem = os.getenv("SSH_PRIVATE_KEY")
        if not private_key_pem:
            raise ValueError("SSH_PRIVATE_KEY environment variable is required")

        self._temp_key_path = self._create_temp_key_file(private_key_pem)

    def _create_temp_key_file(self, private_key_pem: str) -> str:
        """Create temporary file for private key from environment variable"""
        import tempfile

        fd, temp_path = tempfile.mkstemp(prefix="git_sync_key_", suffix=".pem")
        try:
            os.write(fd, private_key_pem.encode("utf-8"))
            os.chmod(temp_path, 0o600)
            # Register cleanup
            atexit.register(lambda: os.unlink(temp_path) if os.path.exists(temp_path) else None)
            return temp_path
        finally:
            os.close(fd)

    @property
    def private_key_path(self) -> str:
        """Get private key path"""
        return self._temp_key_path

    def get_public_key(self) -> str:
        """Extract public key from private key"""
        private_key_pem = os.getenv("SSH_PRIVATE_KEY")
        if not private_key_pem:
            raise ValueError("SSH_PRIVATE_KEY environment variable is required")

        try:
            # Load private key
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None, backend=default_backend()
            )

            # Extract public key
            public_key = private_key.public_key()
            public_ssh = public_key.public_bytes(
                encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH
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
                raise ValueError(f"Invalid private key format in SSH_PRIVATE_KEY: {str(e)}")


class PersistenceManager:
    """Manage file-based state and git repository operations"""

    HASHES_FILE = "document_hashes.json"
    FILEMETA_FILE = "shared_folders.json"
    MIRROR_BASE_DIR = "repos"
    LOCAL_STATE_FILE = "local_state.json"

    def __init__(self, data_dir: str = ".", git_config_file: Optional[str] = None):
        self.data_dir = data_dir
        self.git_repos: Dict[str, git.Repo] = {}  # Now keyed by "relay_id/folder_id"
        self.git_lock = threading.Lock()  # Prevent concurrent git operations

        # Initialize SSH key manager only if SSH_PRIVATE_KEY is set
        self.ssh_key_manager = None
        if os.getenv("SSH_PRIVATE_KEY"):
            try:
                self.ssh_key_manager = SSHKeyManager()
            except Exception as e:
                logger.warning(f"Failed to initialize SSH key manager: {e}")
                logger.warning("Git operations requiring SSH authentication will fail")

        # Initialize git connector configuration
        config_path = git_config_file or os.path.join(self.data_dir, "git_connectors.toml")
        self.git_config = GitConnectorConfig(config_path)

        # In-memory storage for state (these will be loaded/saved per relay)
        self.document_hashes: Dict[str, Dict[str, str]] = {}  # keyed by relay_id then doc_id
        self.filemeta_folders: Dict[
            str, Dict[str, Dict]
        ] = {}  # keyed by relay_id then folder doc_id
        self.local_file_state: Dict[
            str, Dict[str, Dict]
        ] = {}  # keyed by relay_id then folder_id then path

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
        """Initialize Git repositories for all folders (existing + TOML configured)"""
        try:
            logger.info("Initializing Git repositories...")
            initialized_count = 0

            # First: Initialize repos for existing folders with state
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
                        logger.info(f"Creating minimal folder state for {relay_id}/{folder_id} from TOML config")
                        self.filemeta_folders[relay_id][folder_id] = {}
                        
                        # Save the minimal state
                        self.save_persistent_data(relay_id)

                    # Initialize Git repository
                    self.init_git_repo(relay_id, folder_id)
                    
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

                except Exception as e:
                    logger.error(
                        f"Failed to initialize Git repo from TOML for {connector.relay_id}/{connector.shared_folder_id}: {e}"
                    )

            return initialized_count

        except Exception as e:
            logger.error(f"Error initializing Git repositories from TOML: {e}")
            return 0

    def _safe_git_operation(self, func, *args, **kwargs):
        """Execute git operation with locking and error recovery"""
        with self.git_lock:
            # Set up SSH key environment for Git operations
            original_env = os.environ.copy()
            try:
                # Configure Git to use our SSH key if available
                if self.ssh_key_manager:
                    private_key_path = self.ssh_key_manager.private_key_path
                    os.environ[
                        "GIT_SSH_COMMAND"
                    ] = f'ssh -i "{private_key_path}" -o StrictHostKeyChecking=no'

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
            finally:
                # Restore original environment
                os.environ.clear()
                os.environ.update(original_env)

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
        """Load document hashes, filemeta, and local state for a specific relay"""
        # Ensure relay exists in data structures
        if relay_id not in self.document_hashes:
            self.document_hashes[relay_id] = {}
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

    def save_persistent_data(self, relay_id: str):
        """Save document hashes, filemeta, and local state for a specific relay"""
        # Ensure state directory exists
        os.makedirs(self.get_state_dir(relay_id), exist_ok=True)

        try:
            with open(self.get_hashes_file_path(relay_id), "w") as f:
                json.dump(self.document_hashes.get(relay_id, {}), f, indent=2)
        except Exception as e:
            logger.error(f"Error saving document hashes for relay {relay_id}: {e}")

        try:
            with open(self.get_filemeta_file_path(relay_id), "w") as f:
                json.dump(self.filemeta_folders.get(relay_id, {}), f, indent=2)
        except Exception as e:
            logger.error(f"Error saving filemeta for relay {relay_id}: {e}")

        try:
            with open(self.get_local_state_file_path(relay_id), "w") as f:
                json.dump(self.local_file_state.get(relay_id, {}), f, indent=2)
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
        except git.InvalidGitRepositoryError:
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
                    f.write("# Y-Sweet sync repository - content only\n")

                self.git_repos[repo_key].index.add([".gitignore"])
                self.git_repos[repo_key].index.commit("Initial commit")
                print(f"Created initial git commit for folder {folder_id} in relay {relay_id}")

        return self.git_repos[repo_key]

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
                git_repo.create_remote(remote_name, remote_url)
                print(
                    f"Added remote '{remote_name}' for folder {folder_id} in relay {relay_id}: {remote_url}"
                )

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
            if connector:
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
                logger.debug(f"No git connector configured for folder {folder_id} in relay {relay_id}")
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
                if git_repo.is_dirty() or git_repo.untracked_files:
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

            return committed_any

        except Exception as e:
            logger.error(f"Error committing to git: {e}")
            logger.error(f"Git commit traceback: {traceback.format_exc()}")
            return False

    def _push_to_remote(self, repo_key: str, git_repo: git.Repo):
        """Push commits to remote repository if configured"""
        try:
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
                    # Set upstream for first push
                    self._safe_git_operation(
                        lambda: origin.push(current_branch.name, set_upstream=True)
                    )
                    print(
                        f"Git push (set upstream) for repository {repo_key} to {origin.name}/{current_branch.name}"
                    )
                else:
                    # Regular push
                    self._safe_git_operation(lambda: origin.push())
                    print(
                        f"Git push for repository {repo_key} to {origin.name}/{current_branch.name}"
                    )

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

        # Reject paths containing .. components
        if ".." in clean_path:
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

            # Index folders from filemeta_folders
            for folder_id in self.filemeta_folders.get(relay_id, {}).keys():
                relay_index[folder_id] = {
                    "type": "folder",
                    "folder_id": folder_id,
                    "path": None,  # Folders don't have paths, they contain files
                    "metadata": {},
                }

            # Index documents from local_file_state (the authoritative source)
            for folder_id, folder_state in self.local_file_state.get(relay_id, {}).items():
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
