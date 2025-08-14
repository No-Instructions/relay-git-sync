# Relay Server Git Sync

A real-time synchronization bridge between Relay Server collaborative documents and Git repositories, enabling automatic version control and backup for collaborative workspaces.

## What It Does

Relay Server Git Sync monitors collaborative documents in Relay Server and automatically maintains synchronized copies in local Git repositories. When users edit documents in Relay Server, the changes are instantly reflected in the local file system and committed to Git with timestamps.

## Key Features

- **Real-time synchronization** - Webhook-driven updates ensure immediate sync
- **Multi-tenant support** - Handle multiple Relays and Shared Folders simultaneously  
- **Automatic Git versioning** - Timestamped commits preserve change history
- **Conflict-free operations** - Smart handling of renames, moves, and deletions
- **Persistent state management** - Maintains sync integrity across restarts

## Use Cases

- **Team collaboration backup** - Preserve collaborative work in version control
- **Integration workflows** - Connect collaborative editing to existing Git-based processes
- **Document archival** - Maintain historical records of collaborative sessions

## How It Works

1. Relay Server sends webhook notifications when documents change
2. System analyzes changes and plans sync operations
3. Files are updated locally with proper conflict resolution
4. Changes are automatically committed to Git with timestamps
5. Process repeats continuously for real-time synchronization


## Setup

### Deployment

Use the provided docker container as a starting point.

```
docker pull docker.system3.md/relay-git-sync:latest
```

### Authentication Setup

The server supports two authentication methods:

#### Method 1: Simple Shared Secret (Recommended)

For most deployments, use a shared secret for webhook authentication:

1. **Generate a shared secret:**

```bash
openssl rand -base64 32
```

2. **Set the environment variable:**

```bash
export WEBHOOK_SECRET="your-generated-secret"
```

3. **Configure your webhook provider** with:
```
RELAY_SERVER_WEBHOOK_CONFIG=[{"url": "https://your-git-sync-server.com/webhooks", "auth_token": "your-generated-secret" }]
```

#### Method 2: HMAC Signatures (Webhook Delivery Service)

Use this method if you are using the webhook delivery service (svix).
To use webhook signatures, use a secret starting with `whsec_`:

```bash
export WEBHOOK_SECRET=whsec_your_signing_secret
```

### SSH Key Setup

SSH keys must be provided via the `SSH_PRIVATE_KEY` environment variable.

First, generate an SSH key pair externally:

```bash
ssh-keygen -t ed25519 -f git_sync_key -N ""
```

Then set the environment variable:

```bash
export SSH_PRIVATE_KEY="$(cat git_sync_key)"
```

View the public key from the private key:

```bash
uv run cli.py ssh show-pubkey
```

Add the public key to your Git hosting service (GitHub, GitLab, etc.) as a deploy key with write permissions.

### Configuring a Git Remote

Create `git_connectors.toml` in your data directory to configure git repositories:

```toml
[[git_connector]]
shared_folder_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
relay_id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
url = "https://github.com/example/repository.git"
branch = "main"
remote_name = "origin"
prefix = ""  # Optional: subdirectory within repo
```

The system will automatically create repositories and configure remotes on startup.


### Running the Server

Start the webhook server:

```bash
# Set required environment variables
export RELAY_SERVER_URL=https://your-relay-server.com
export RELAY_SERVER_API_KEY=your-server-api-key        # From System 3 team
export WEBHOOK_SECRET=whs_...                          # From step 1 above
export SSH_PRIVATE_KEY="$(cat git_sync_key)"          # Required for Git push operations
export RELAY_GIT_DATA_DIR=/path/to/data                # Optional, defaults to current directory

# Start the server
python app.py --port 8000 --commit-interval 10
```

#### Command Line Options

- `--port`: HTTP server port (default: 8000)
- `--commit-interval`: Git commit interval in seconds (default: 10)
- `--relay-server-url`: Relay server URL (or set `RELAY_SERVER_URL`)
- `--relay-server-api-key`: API key for Relay server (or set `RELAY_SERVER_API_KEY`)
- `--data-dir`: Data storage directory (default: from `RELAY_GIT_DATA_DIR` env var or current directory)
- `--webhook-secret`: Webhook secret (or set `WEBHOOK_SECRET`)


### Manual Sync

You can also perform one-time syncs within your container by using the CLI:

```bash
uv run cli.py sync --relay-id <relay-uuid> --folder-id <folder-uuid>
```


### Directory Structure

The server organizes data as follows:

```
data-dir/
├── repos/
│   └── <relay-id>/
│       └── <folder-id>/          # Git repository for each folder
└── state/
    └── <relay-id>/
        ├── document_hashes.json  # Change tracking
        ├── shared_folders.json   # Folder metadata
        └── local_state.json      # File state per folder
```

## API Documentation

The server provides both webhook endpoints for Relay Server integration and public API endpoints for management.

**Interactive Documentation:** Visit `/docs` when the server is running for interactive Swagger UI documentation.

**OpenAPI Specification:** See [`openapi.yaml`](./openapi.yaml) for complete API documentation with request/response schemas, authentication details, and examples.

**Key Endpoints:**
- `GET /health` - Health check (public)  
- `GET /api/pubkey` - SSH public key retrieval (public)
- `GET /docs` - Interactive API documentation (public)
- `GET /openapi.yaml` - OpenAPI specification (public)
