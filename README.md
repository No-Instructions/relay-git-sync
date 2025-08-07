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

Perfect for teams wanting the benefits of collaborative editing with the reliability and history of Git version control.

## Setup

### Deployment

Use the provided docker container as a starting point.

### Authentication Setup

The server supports two authentication methods:

#### Method 1: Bearer Token Authentication (Recommended)

This is the simpler approach using JWT tokens:

1. **Generate a webhook secret:**
```bash
python cli.py webhook keygen
```
This outputs a webhook secret like `whs_<base64-string>`. Set it in your environment:
```bash
export WEBHOOK_SECRET=whs_...
```

2. **Generate a webhook token for Relay Server:**
```bash
python cli.py webhook token create --name "production"
```
This creates a JWT token. You can optionally use the --expires flag to set expiry in days.

3. **Configure Relay Server webhooks** with the token:
```json
[
  {
    "prefix": "85a06712-af14-47bc-a859-e8106cc786e8",
    "url": "https://your-git-sync-server.com/webhooks",
    "timeout_ms": 5000,
    "auth_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
  }
]
```

#### Method 2: Svix HMAC Signatures

For Svix-compatible webhook signatures, use a secret starting with `whsec_`:
```bash
export WEBHOOK_SECRET=whsec_your_signing_secret
```

### SSH Key Setup

Generate SSH keys for Git repository access:

```bash
python cli.py ssh keygen
```

Add the public key to your Git hosting service (GitHub, GitLab, etc.) as a deploy key with write permissions.

### Running the Server

Start the webhook server:

```bash
# Set required environment variables
export RELAY_SERVER_URL=https://your-relay-server.com
export RELAY_SERVER_API_KEY=your-api-key
export WEBHOOK_SECRET=whs_...  # From step 1 above
export RELAY_GIT_DATA_DIR=/path/to/data  # Optional, defaults to current directory

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

You can also perform one-time syncs using the CLI:

```bash
# Sync all folders for a relay
python cli.py sync --relay-id <relay-uuid> --relay-server-url https://your-relay-server.com

# Sync specific folder
python cli.py sync --relay-id <relay-uuid> --folder-id <folder-uuid> --relay-server-url https://your-relay-server.com
```

### Directory Structure

The server organizes data as follows:

```
data-dir/
├── repos/
│   └── <relay-id>/
│       └── <folder-id>/          # Git repository for each folder
├── state/
│   └── <relay-id>/
│       ├── document_hashes.json  # Change tracking
│       ├── shared_folders.json   # Folder metadata
│       └── local_state.json      # File state per folder
└── ssh_keys/                     # SSH keys for Git access
    ├── id_rsa
    └── id_rsa.pub
```
