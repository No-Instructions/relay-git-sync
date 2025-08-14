#!/usr/bin/env python3

import os
import logging
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # Fallback for older Python versions
    except ImportError:
        logger.error("TOML parsing not available. Install tomli for Python < 3.11")
        tomllib = None


@dataclass
class GitConnector:
    """Configuration for a git connector linking a shared folder to a git repository"""

    shared_folder_id: str
    relay_id: str
    url: str
    branch: str = "main"
    remote_name: str = "origin"
    prefix: str = ""

    def __post_init__(self):
        """Validate the configuration after initialization"""
        if not self.shared_folder_id:
            raise ValueError("shared_folder_id is required")
        if not self.relay_id:
            raise ValueError("relay_id is required")
        if not self.url:
            raise ValueError("url is required")
        if not self.branch:
            raise ValueError("branch is required")
        if not self.remote_name:
            raise ValueError("remote_name is required")

        # Validate UUID format (basic check)
        if len(self.shared_folder_id.split("-")) != 5:
            raise ValueError(f"Invalid shared_folder_id format: {self.shared_folder_id}")
        if len(self.relay_id.split("-")) != 5:
            raise ValueError(f"Invalid relay_id format: {self.relay_id}")


class GitConnectorConfig:
    """Manages git connector configuration from TOML files"""

    def __init__(self, config_file: Optional[str] = None):
        self.config_file = config_file or "git_connectors.toml"
        self.connectors: List[GitConnector] = []
        self._load_config()

    def _load_config(self):
        """Load git connectors from TOML configuration file"""
        if tomllib is None:
            logger.warning("TOML parsing not available - git connectors disabled")
            return

        config_path = Path(self.config_file)
        if not config_path.exists():
            logger.info(f"Git connector config file not found: {config_path}")
            return

        try:
            with open(config_path, "rb") as f:
                config_data = tomllib.load(f)

            # Parse git_connector entries
            git_connectors = config_data.get("git_connector", [])
            if not isinstance(git_connectors, list):
                logger.error("git_connector must be an array in TOML config")
                return

            self.connectors = []
            for i, connector_data in enumerate(git_connectors):
                try:
                    connector = GitConnector(
                        shared_folder_id=connector_data["shared_folder_id"],
                        relay_id=connector_data["relay_id"],
                        url=connector_data["url"],
                        branch=connector_data.get("branch", "main"),
                        remote_name=connector_data.get("remote_name", "origin"),
                        prefix=connector_data.get("prefix", ""),
                    )
                    self.connectors.append(connector)
                    logger.info(
                        f"Loaded git connector: relay={connector.relay_id}, "
                        f"folder={connector.shared_folder_id}, url={connector.url}"
                    )
                except KeyError as e:
                    logger.error(f"Missing required field in git_connector[{i}]: {e}")
                except ValueError as e:
                    logger.error(f"Invalid git_connector[{i}] configuration: {e}")

        except Exception as e:
            logger.error(f"Error loading git connector config from {config_path}: {e}")

    def get_connector_for_folder(self, relay_id: str, folder_id: str) -> Optional[GitConnector]:
        """Get git connector configuration for a specific folder"""
        for connector in self.connectors:
            if connector.relay_id == relay_id and connector.shared_folder_id == folder_id:
                return connector
        return None

    def get_connectors_for_relay(self, relay_id: str) -> List[GitConnector]:
        """Get all git connector configurations for a specific relay"""
        return [c for c in self.connectors if c.relay_id == relay_id]

    def add_connector(self, connector: GitConnector):
        """Add a new git connector configuration"""
        # Remove existing connector with same relay_id/folder_id if it exists
        self.connectors = [
            c
            for c in self.connectors
            if not (
                c.relay_id == connector.relay_id
                and c.shared_folder_id == connector.shared_folder_id
            )
        ]
        self.connectors.append(connector)

    def remove_connector(self, relay_id: str, folder_id: str) -> bool:
        """Remove a git connector configuration"""
        original_count = len(self.connectors)
        self.connectors = [
            c
            for c in self.connectors
            if not (c.relay_id == relay_id and c.shared_folder_id == folder_id)
        ]
        return len(self.connectors) < original_count

    def save_config(self):
        """Save current git connectors to TOML configuration file"""
        if tomllib is None:
            logger.error("TOML parsing not available - cannot save config")
            return False

        try:
            # Convert connectors to TOML format
            config_data = {
                "git_connector": [
                    {
                        "shared_folder_id": c.shared_folder_id,
                        "relay_id": c.relay_id,
                        "url": c.url,
                        "branch": c.branch,
                        "remote_name": c.remote_name,
                        "prefix": c.prefix,
                    }
                    for c in self.connectors
                ]
            }

            # For saving, we need tomlkit or similar - for now just document the format
            logger.warning("TOML saving not implemented - manually edit git_connectors.toml")
            logger.info(f"Config would contain {len(self.connectors)} connectors")
            return False

        except Exception as e:
            logger.error(f"Error saving git connector config: {e}")
            return False

    def validate_config(self) -> List[str]:
        """Validate all git connector configurations and return error messages"""
        errors = []

        # Check for duplicate relay_id/folder_id combinations
        seen_combinations = set()
        for i, connector in enumerate(self.connectors):
            combo = (connector.relay_id, connector.shared_folder_id)
            if combo in seen_combinations:
                errors.append(
                    f"Duplicate git_connector[{i}]: relay_id={connector.relay_id}, "
                    f"shared_folder_id={connector.shared_folder_id}"
                )
            seen_combinations.add(combo)

            # Validate URL format
            if not connector.url.startswith(("http://", "https://", "git@", "ssh://")):
                errors.append(
                    f"Invalid URL format in git_connector[{i}]: {connector.url}. "
                    f"Must start with http://, https://, git@, or ssh://"
                )

            # Validate branch name (basic check)
            if not connector.branch or "/" in connector.branch.split("/")[-1]:
                errors.append(f"Invalid branch name in git_connector[{i}]: {connector.branch}")

            # Validate remote name
            if not connector.remote_name or " " in connector.remote_name:
                errors.append(f"Invalid remote name in git_connector[{i}]: {connector.remote_name}")

        return errors

    def reload_config(self):
        """Reload configuration from file"""
        self._load_config()

    def get_config_file_path(self) -> str:
        """Get the full path to the configuration file"""
        return os.path.abspath(self.config_file)

    def create_example_config(self):
        """Create an example configuration file"""
        example_content = """# Git Connector Configuration
# Configure git repositories to sync with shared folders

[[git_connector]]
shared_folder_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
relay_id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
url = "https://github.com/example/repository.git"
branch = "main"
remote_name = "origin"
prefix = ""  # Optional: subdirectory within repo (e.g., "docs" or "content/posts")

# [[git_connector]]
# shared_folder_id = "another-folder-uuid"
# relay_id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
# url = "git@github.com:example/private-repo.git"
# branch = "develop"
# remote_name = "origin"
# prefix = "content/docs"  # Sync to content/docs/ subdirectory
"""

        config_path = Path(self.config_file)
        if config_path.exists():
            logger.warning(f"Config file already exists: {config_path}")
            return False

        try:
            with open(config_path, "w") as f:
                f.write(example_content)
            logger.info(f"Created example git connector config: {config_path}")
            return True
        except Exception as e:
            logger.error(f"Error creating example config: {e}")
            return False
