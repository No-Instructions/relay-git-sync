#!/usr/bin/env python3

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from git_host_keys import (
    KnownHostKeyFetchError,
    bundled_known_hosts_for_host,
    dedupe_preserving_order,
    fetch_known_hosts_for_host,
    is_known_provider_host,
    known_hosts_cover_host,
)

logger = logging.getLogger(__name__)

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # Fallback for older Python versions
    except ImportError:
        logger.error("TOML parsing not available. Install tomli for Python < 3.11")
        tomllib = None


def ssh_host_from_git_url(url: str) -> Optional[str]:
    if not url:
        return None

    stripped = url.strip()
    parsed = urlparse(stripped)
    if parsed.scheme == "ssh":
        return parsed.hostname
    if parsed.scheme:
        return None

    match = re.match(r"^(?:[^@]+@)?([^:/]+):", stripped)
    if match:
        return match.group(1)

    return None


def _known_hosts_cache_path(config_file: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_file)), ".known_hosts_cache.json")


def _load_known_hosts_cache(cache_path: str) -> Dict[str, List[str]]:
    try:
        with open(cache_path, "r") as f:
            cache = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Ignoring unreadable known_hosts cache {cache_path}: {e}")
        return {}

    if not isinstance(cache, dict):
        return {}
    return {
        host: entries
        for host, entries in cache.items()
        if isinstance(host, str)
        and isinstance(entries, list)
        and all(isinstance(entry, str) for entry in entries)
    }


def _save_known_hosts_cache(cache_path: str, cache: Dict[str, List[str]]) -> None:
    try:
        temp_path = f"{cache_path}.tmp"
        with open(temp_path, "w") as f:
            json.dump(cache, f, indent=2)
        os.replace(temp_path, cache_path)
    except Exception as e:
        logger.warning(f"Could not write known_hosts cache {cache_path}: {e}")


def _known_hosts_entries_for_urls(
    urls: List[str],
    explicit_known_hosts: List[str],
    cache_path: Optional[str] = None,
) -> Tuple[List[str], Dict[str, str]]:
    entries: List[str] = []
    errors: Dict[str, str] = {}
    fetched_hosts = set()
    cache = _load_known_hosts_cache(cache_path) if cache_path else {}
    cache_dirty = False
    for url in urls:
        host = ssh_host_from_git_url(url)
        if (
            not host
            or host in fetched_hosts
            or not is_known_provider_host(host)
            or known_hosts_cover_host(explicit_known_hosts, host)
        ):
            continue

        fetched_hosts.add(host)
        try:
            host_entries = fetch_known_hosts_for_host(host)
        except KnownHostKeyFetchError as e:
            # Fall back to the last successfully fetched keys so a provider
            # outage or rate limit does not prevent startup.
            cached_entries = cache.get(host, [])
            if known_hosts_cover_host(cached_entries, host):
                logger.warning(f"Using cached known_hosts for {host} after fetch failure: {e}")
                entries.extend(cached_entries)
            else:
                bundled_entries = bundled_known_hosts_for_host(host)
                if known_hosts_cover_host(bundled_entries, host):
                    logger.warning(f"Using bundled known_hosts for {host} after fetch failure: {e}")
                    entries.extend(bundled_entries)
                else:
                    errors[host] = str(e)
            continue

        entries.extend(host_entries)
        if cache.get(host) != host_entries:
            cache[host] = host_entries
            cache_dirty = True

    if cache_path and cache_dirty:
        _save_known_hosts_cache(cache_path, cache)

    return dedupe_preserving_order(entries), errors


def default_git_config_file(data_dir: str = ".", git_config_file: Optional[str] = None) -> str:
    return git_config_file or os.path.join(data_dir, "git_connectors.toml")


def relay_ids_from_config(config_file: str) -> List[str]:
    config = GitConnectorConfig(config_file)
    relay_ids = set()
    if config.relay_id:
        relay_ids.add(config.relay_id)
    relay_ids.update(connector.relay_id for connector in config.connectors)
    return sorted(relay_ids)


def relay_url_from_config(config_file: str) -> Optional[str]:
    return GitConnectorConfig(config_file).relay_url


def webhook_url_from_config(config_file: str) -> Optional[str]:
    return GitConnectorConfig(config_file).webhook_url


def resolve_webhook_url(
    explicit_url: Optional[str],
    *,
    data_dir: str = ".",
    git_config_file: Optional[str] = None,
) -> Optional[str]:
    if explicit_url:
        return explicit_url

    config_file = default_git_config_file(data_dir, git_config_file)
    return webhook_url_from_config(config_file)


def resolve_relay_url(
    explicit_url: Optional[str],
    *,
    data_dir: str = ".",
    git_config_file: Optional[str] = None,
    env_var: str = "RELAY_SERVER_URL",
) -> Optional[str]:
    if explicit_url:
        return explicit_url

    config_file = default_git_config_file(data_dir, git_config_file)
    config_url = relay_url_from_config(config_file)
    if config_url:
        return config_url

    return os.getenv(env_var)


def resolve_relay_id(
    explicit_relay_id: Optional[str],
    *,
    data_dir: str = ".",
    git_config_file: Optional[str] = None,
    env_var: str = "RELAY_ID",
) -> Optional[str]:
    if explicit_relay_id:
        return explicit_relay_id

    config_file = default_git_config_file(data_dir, git_config_file)
    config_relay_ids = relay_ids_from_config(config_file)
    if len(config_relay_ids) == 1:
        return config_relay_ids[0]

    if len(config_relay_ids) > 1:
        raise ValueError(
            f"Multiple relay IDs found in {config_file}. Pass --relay-id to choose one."
        )

    return os.getenv(env_var)


@dataclass
class GitConnector:
    """Configuration for a git connector linking a shared folder to a git repository"""

    shared_folder_id: str
    relay_id: str
    url: str = ""
    branch: str = "main"
    remote_name: str = "origin"
    prefix: str = ""

    def __post_init__(self):
        """Validate the configuration after initialization"""
        if not self.shared_folder_id:
            raise ValueError("shared_folder_id is required")
        if not self.relay_id:
            raise ValueError("relay_id is required")
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
        self.relay_id: Optional[str] = None
        self.relay_url: Optional[str] = None
        self.webhook_url: Optional[str] = None
        self.known_hosts: List[str] = []
        self.explicit_known_hosts: List[str] = []
        self.known_hosts_fetch_errors: Dict[str, str] = {}
        self.connectors: List[GitConnector] = []
        self.authors: Dict[str, str] = {}
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

            relay_config = config_data.get("relay", {})
            if relay_config:
                if not isinstance(relay_config, dict):
                    logger.error("relay must be a table in TOML config")
                    return
                self.relay_id = relay_config.get("id")
                self.relay_url = relay_config.get("url")

            webhook_config = config_data.get("webhook", {})
            if webhook_config:
                if not isinstance(webhook_config, dict):
                    logger.error("webhook must be a table in TOML config")
                    return
                self.webhook_url = webhook_config.get("url")

            known_hosts = config_data.get("known_hosts", [])
            if known_hosts:
                if not isinstance(known_hosts, list) or not all(
                    isinstance(entry, str) for entry in known_hosts
                ):
                    logger.error("known_hosts must be an array of strings in TOML config")
                    return
                self.explicit_known_hosts = known_hosts
                self.known_hosts = known_hosts

            authors = config_data.get("authors", {})
            if authors:
                if not isinstance(authors, dict) or not all(
                    isinstance(v, str) for v in authors.values()
                ):
                    logger.error("authors must be a table of 'Name <email>' strings in TOML config")
                    return
                self.authors = authors

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
                        relay_id=connector_data.get("relay_id") or self.relay_id,
                        url=connector_data.get("url", ""),
                        branch=connector_data.get("branch", "main"),
                        remote_name=connector_data.get("remote_name", "origin"),
                        prefix=connector_data.get("prefix", ""),
                    )
                    self.connectors.append(connector)
                    logger.debug(
                        f"Loaded git connector: relay={connector.relay_id}, "
                        f"folder={connector.shared_folder_id}, url={connector.url}"
                    )
                except KeyError as e:
                    logger.error(f"Missing required field in git_connector[{i}]: {e}")
                except ValueError as e:
                    logger.error(f"Invalid git_connector[{i}] configuration: {e}")

            inferred_known_hosts, self.known_hosts_fetch_errors = _known_hosts_entries_for_urls(
                [connector.url for connector in self.connectors],
                self.explicit_known_hosts,
                cache_path=_known_hosts_cache_path(self.config_file),
            )
            self.known_hosts = dedupe_preserving_order(
                self.explicit_known_hosts + inferred_known_hosts
            )

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
                "relay": {
                    "id": self.relay_id,
                    "url": self.relay_url,
                },
                "webhook": {
                    "url": self.webhook_url,
                },
                "known_hosts": self.explicit_known_hosts,
                "git_connector": [
                    {
                        "shared_folder_id": c.shared_folder_id,
                        "url": c.url,
                        "branch": c.branch,
                        "remote_name": c.remote_name,
                        "prefix": c.prefix,
                    }
                    for c in self.connectors
                ],
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

        if self.relay_url and not self.relay_url.startswith(("http://", "https://")):
            errors.append(
                f"Invalid relay.url: {self.relay_url}. Must start with http:// or https://"
            )

        if self.webhook_url and not self.webhook_url.startswith(("http://", "https://")):
            errors.append(
                f"Invalid webhook.url: {self.webhook_url}. Must start with http:// or https://"
            )

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

            # Validate URL format when a remote is configured. Missing URL means
            # local-only snapshots: the connector still commits, but never pushes.
            if connector.url and not connector.url.startswith(
                ("http://", "https://", "git@", "ssh://")
            ):
                errors.append(
                    f"Invalid URL format in git_connector[{i}]: {connector.url}. "
                    f"Must start with http://, https://, git@, or ssh://"
                )

            ssh_host = ssh_host_from_git_url(connector.url)
            if ssh_host and not known_hosts_cover_host(self.explicit_known_hosts, ssh_host):
                if is_known_provider_host(ssh_host):
                    fetch_error = self.known_hosts_fetch_errors.get(ssh_host)
                    if fetch_error:
                        errors.append(
                            "Unable to fetch known_hosts for SSH host in "
                            f"git_connector[{i}]: {ssh_host}: {fetch_error}"
                        )
                    elif not known_hosts_cover_host(self.known_hosts, ssh_host):
                        errors.append(
                            "Trusted provider returned no known_hosts entry for SSH host in "
                            f"git_connector[{i}]: {ssh_host}"
                        )
                else:
                    errors.append(
                        f"Missing known_hosts entry for SSH host in git_connector[{i}]: {ssh_host}"
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

# SSH known host keys for custom Git hosts.
# GitHub, GitLab.com, and Bitbucket Cloud SSH URLs automatically fetch published host keys.
# Add explicit entries here for offline deployments, custom hosts, or self-hosted Git hosts.
# known_hosts = [
#     "git.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...",
# ]

[relay]
id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
url = "https://your-relay-server.com"

# Optional: only needed when Relay Server should POST webhooks to Git Sync.
# [webhook]
# url = "https://your-git-sync-server.com/webhooks"

[[git_connector]]
shared_folder_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
url = "https://github.com/example/repository.git"
branch = "main"
remote_name = "origin"
prefix = ""  # Optional: subdirectory within repo (e.g., "docs" or "content/posts")

# Local-only snapshot repository: commits locally without pushing to a remote
# [[git_connector]]
# shared_folder_id = "another-folder-uuid"
# prefix = "snapshots"

# [[git_connector]]
# shared_folder_id = "another-folder-uuid"
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
