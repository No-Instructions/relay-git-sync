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
   RELAY_SERVER_WEBHOOK_CONFIG=[{
   "url": "https://your-git-sync-server.com/webhooks",
   "auth_token": "your-generated-secret"
   }]

Note: The server matches the exact value in the Bearer token against WEBHOOK_SECRET.

#### Method 2: HMAC Signatures (Webhook Delivery Service)

Use this method if you are using the webhook delivery service (svix).
To use webhook signatures, use a secret starting with `whsec_`:

```bash
export WEBHOOK_SECRET=whsec_your_signing_secret
```

### Running the Server

Start the webhook server:

```bash
# Set required environment variables
export RELAY_SERVER_URL=https://your-relay-server.com
export RELAY_SERVER_API_KEY=your-server-api-key        # From System 3 team
export WEBHOOK_SECRET=whs_...                          # From step 1 above
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

### SSH Key Setup

Relay Git Sync will automatically generate SSH keys on startup.
You can view the public key within your container by running:

```bash
uv run cli.py ssh show-pubkey
```

Add the public key to your Git hosting service (GitHub, GitLab, etc.) as a deploy key with write permissions.

### Configuring a Git Remote

Once you have performed manual sync (or successfully received a folder-related webhook event) you can add a git remote.

```
# Navigate to the git repo
cd $RELAY_GIT_DATA_DIR/repos/<relay-guid>/<shared-folder-guid>/

# Add a remote
git remote add origin <remote url>
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
