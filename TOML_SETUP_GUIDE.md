# Git Connectors TOML Setup Guide

This guide shows how to configure git repositories using TOML configuration files for automatic folder creation and git remote setup.

## Overview

With TOML configuration, you can:
- Define git repositories for shared folders **before** they exist
- Automatically create git repositories on startup
- Configure git remotes, branches, and authentication
- Version control your git connector configuration

## Configuration File Format

Create `git_connectors.toml` in your data directory:

```toml
[[git_connector]]
shared_folder_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
relay_id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
url = "https://github.com/example/repository.git"
branch = "main"
remote_name = "origin"

[[git_connector]]
shared_folder_id = "12345678-1234-5678-9abc-123456789abc"
relay_id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
url = "git@github.com:example/private-repo.git"
branch = "develop"
remote_name = "origin"
```

## Fresh Container Setup

### 1. Create Configuration

```bash
# Create example configuration
python cli.py git init

# Edit the generated file with your values
vim git_connectors.toml
```

### 2. Start Container

When you start the container with the TOML file populated:

```bash
# The system will automatically:
# 1. Parse git_connectors.toml
# 2. Create minimal folder state for each connector
# 3. Initialize git repositories in repos/<relay_id>/<folder_id>/
# 4. Configure git remotes with specified URLs
# 5. Set up branches and remote names

python app.py --data-dir /data
```

### 3. Verify Setup

```bash
# List configured connectors
python cli.py git list

# Manually trigger creation (if needed)
python cli.py git sync

# Validate configuration
python cli.py git validate
```

## Directory Structure Created

For each connector in your TOML:

```
/data/
├── git_connectors.toml
├── state/
│   └── 6ba7b810-9dad-11d1-80b4-00c04fd430c8/
│       ├── document_hashes.json
│       ├── shared_folders.json (contains minimal folder state)
│       └── local_state.json
└── repos/
    └── 6ba7b810-9dad-11d1-80b4-00c04fd430c8/
        ├── f47ac10b-58cc-4372-a567-0e02b2c3d479/
        │   ├── .git/ (initialized with remote configured)
        │   └── .gitignore
        └── 12345678-1234-5678-9abc-123456789abc/
            ├── .git/ (initialized with remote configured)
            └── .gitignore
```

## SSH Authentication

For private repositories using SSH:

```bash
# 1. Set your SSH private key
export SSH_PRIVATE_KEY="$(cat ~/.ssh/id_ed25519)"

# 2. Get the public key to add to your git hosting service
python cli.py ssh show-pubkey

# 3. Add the public key to GitHub/GitLab as a deploy key
```

## CLI Commands

### Configuration Management
```bash
# Create example config
python cli.py git init

# List all connectors
python cli.py git list

# Add new connector
python cli.py git add \
  --relay-id 6ba7b810-9dad-11d1-80b4-00c04fd430c8 \
  --folder-id f47ac10b-58cc-4372-a567-0e02b2c3d479 \
  --url https://github.com/user/repo.git \
  --branch main

# Remove connector
python cli.py git remove \
  --relay-id 6ba7b810-9dad-11d1-80b4-00c04fd430c8 \
  --folder-id f47ac10b-58cc-4372-a567-0e02b2c3d479

# Validate configuration
python cli.py git validate
```

### Repository Management
```bash
# Create repositories from TOML (manual trigger)
python cli.py git sync

# Sync specific relay/folder (requires relay server)
python cli.py sync --relay-id 6ba7b810-9dad-11d1-80b4-00c04fd430c8

# Show SSH public key
python cli.py ssh show-pubkey
```

## Startup Behavior

1. **Existing Folders**: Repositories are created for folders that already have state
2. **TOML Connectors**: Repositories are created for ALL connectors in TOML, even without prior state
3. **Remote Configuration**: Git remotes are automatically configured from TOML settings
4. **State Creation**: Minimal folder state is created if it doesn't exist

## Common Patterns

### Multiple Repositories per Relay
```toml
# Different folders -> different repos
[[git_connector]]
shared_folder_id = "docs-folder-uuid"
relay_id = "my-relay-uuid"
url = "https://github.com/company/docs.git"

[[git_connector]]
shared_folder_id = "code-folder-uuid"
relay_id = "my-relay-uuid"
url = "https://github.com/company/source.git"
```

### Different Branches
```toml
[[git_connector]]
shared_folder_id = "prod-folder-uuid"
relay_id = "my-relay-uuid"
url = "https://github.com/company/config.git"
branch = "production"

[[git_connector]]
shared_folder_id = "staging-folder-uuid"
relay_id = "my-relay-uuid"
url = "https://github.com/company/config.git"
branch = "staging"
```

### Multiple Relays
```toml
[[git_connector]]
shared_folder_id = "shared-folder-uuid"
relay_id = "dev-relay-uuid"
url = "https://github.com/company/dev-content.git"

[[git_connector]]
shared_folder_id = "shared-folder-uuid"
relay_id = "prod-relay-uuid"
url = "https://github.com/company/prod-content.git"
```

## Troubleshooting

### Configuration Issues
```bash
# Check configuration validity
python cli.py git validate

# Check if repositories were created
python cli.py git list
ls -la repos/
```

### Missing Repositories
```bash
# Force creation from TOML
python cli.py git sync

# Check logs for errors
python app.py --verbose
```

### Authentication Issues
```bash
# Verify SSH key is set
echo $SSH_PRIVATE_KEY | head -c 50

# Test SSH connection
ssh -T git@github.com

# Check git remote configuration
cd repos/relay-id/folder-id/
git remote -v
```

This TOML-based setup enables infrastructure-as-code for git repository configuration, making container deployments predictable and version-controlled.