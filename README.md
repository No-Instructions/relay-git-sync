# Relay Server Git Sync

A real-time synchronization bridge between Relay Server collaborative documents and Git repositories, enabling automatic version control and backup for collaborative workspaces.

## What It Does

Relay Server Git Sync monitors collaborative documents in Relay Server over websocket subscriptions and automatically maintains synchronized copies in local Git repositories. When users edit documents in Relay Server, the changes are reflected in the local file system and committed to Git with timestamps.

## Key Features

- **Real-time synchronization** - Websocket subscriptions receive live document updates
- **Automatic Git versioning** - Timestamped commits preserve change history
- **Conflict-free operations** - Smart handling of renames, moves, and deletions
- **Persistent state management** - Maintains sync integrity across restarts

## Use Cases

- **Team collaboration backup** - Preserve collaborative work in version control
- **Integration workflows** - Connect collaborative editing to existing Git-based processes
- **Document archival** - Maintain historical records of collaborative sessions

## How It Works

1. Git Sync subscribes to configured Shared Folders over Relay websocket connections
2. Subdocument index heads identify which documents actually changed
3. Folder metadata is diffed for incremental creates, renames, moves, and deletes
4. Only changed document bodies are fetched and written locally
5. Changes are automatically committed to Git with timestamps


## Setup

### Deployment

Use the provided docker container as a starting point.

```
docker pull docker.system3.md/relay-git-sync:latest
```

### Git Sync Configuration

Create `git_connectors.toml` first. This file is the sync plan: each entry maps one
Shared Folder to one local Git repository. Add `url` when that repository should also
push to a remote.

```toml
[relay]
url = "https://your-relay-server.com"
id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

[[git_connector]]
shared_folder_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
url = "git@github.com:example/repository.git"
branch = "main"
remote_name = "origin"
prefix = ""  # Optional subdirectory inside the repository

# Local-only snapshots: commits are kept in the local Git repo and are not pushed
[[git_connector]]
shared_folder_id = "12345678-1234-5678-9abc-123456789abc"
prefix = "shared"
```

Add one `[[git_connector]]` per Shared Folder you want Git Sync to mirror. Omit `url`
for local-only snapshots.

### Event Transport

Websocket listening is the default transport. Git Sync opens an outbound connection to
Relay Server and receives change events there. This is the simplest deployment because
Git Sync does not need to accept inbound connections from Relay Server.

Git Sync sends websocket keepalive pings and uses Relay's subdocument index for
reconnect catch-up. Index heads are persisted locally, so reconnecting compares the
known documents cheaply and fetches only documents whose heads advanced.

Webhook transport is available when you intentionally deploy Git Sync behind a stable URL
that Relay Server can reach. Webhooks avoid keeping a long-lived listener connected and
fit deployments that wake on inbound HTTP requests, but they require network routing from
Relay Server to Git Sync, TLS, and webhook authentication.

To enable webhook transport, add the exact Git Sync webhook endpoint:

```toml
[webhook]
url = "https://your-git-sync-server.com/webhooks"
```

`git_connectors.toml` stores the webhook endpoint URL. The setup output generates the
matching `WEBHOOK_SECRET` for the Git Sync runtime environment.

The Relay API key is not scoped to a Shared Folder; it is scoped to the relay prefix, so
one API key covers the configured Shared Folders for that relay.

Generate the Relay auth setup after the TOML file exists:

```bash
uv run git-sync generate-auth
```

`git-sync setup` is the same command. Git Sync reads `[relay].url`, `[relay].id`,
and optional `[webhook].url` from `<data-dir>/git_connectors.toml`. Use
`-c ./path/to/git_connectors.toml` only when the file is somewhere else.

The setup output gives you:

- a `[[auth]]` block for Relay Server `relay.toml`
- a `RELAY_SERVER_API_KEY` value for Git Sync
- if `[webhook].url` is configured, a `[[webhooks]]` block and `WEBHOOK_SECRET`

Runtime API keys are only read from `RELAY_SERVER_API_KEY`; do not pass them as command
arguments when running the server or sync command.

API keys do not expire unless you pass `--expires-days`.
Use `--json` for machine-readable output.
Inspect an existing API key with `uv run git-sync inspect-token "$RELAY_SERVER_API_KEY"`.
Pass `--webhook-url https://your-git-sync-server.com/webhooks` only to override
`[webhook].url`.

### Webhook Authentication

When `[webhook].url` is configured, the setup output generates a plain shared webhook
secret. Add the generated `[[webhooks]]` block to Relay Server `relay.toml`, and set the
generated `WEBHOOK_SECRET` value in the Git Sync deployment.

If `WEBHOOK_SECRET` is unset, `/webhooks` is not mounted.

#### Signed Webhooks (`whsec_*`)

Use this only when your webhook delivery provider signs requests and gives you a `whsec_*`
signing secret. Native Relay Server webhooks use the generated shared secret above.
Install the `svix` extra before using that secret format:

```bash
uv sync --extra svix
```

Then configure the signing secret:

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
uv run git-sync ssh show-pubkey
```

Add the public key to your Git hosting service (GitHub, GitLab, etc.) as a deploy key with write permissions.

GitHub, GitLab.com, and Bitbucket Cloud SSH remotes automatically fetch the providers'
published host keys. For offline deployments, custom hosts, or self-hosted Git hosts,
add host keys to top-level `known_hosts` in `git_connectors.toml`:

```toml
known_hosts = [
  "git.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleHostKey",
]
```

For custom hosts, copy the provider's published known_hosts entries or get host key
lines with `ssh-keyscan <git-host>`, then verify the fingerprint against your Git
provider before deploying. Explicit `known_hosts` entries override provider fetching
for that host. HTTPS Git remotes and local-only connectors do not use SSH host keys.

### Running the Server

Start the server:

```bash
# Set required environment variables
export RELAY_SERVER_API_KEY=your-server-api-key        # Generated by the setup output
export WEBHOOK_SECRET=your-webhook-secret              # Generated by the setup output
export SSH_PRIVATE_KEY="$(cat git_sync_key)"          # Required for Git push operations
export RELAY_GIT_DATA_DIR=/path/to/data                # Contains git_connectors.toml

# Start the server
python app.py --port 8000 --commit-interval 10
```

The Docker image treats container command arguments as app arguments:

```bash
docker run \
  -e RELAY_SERVER_API_KEY="$RELAY_SERVER_API_KEY" \
  -e WEBHOOK_SECRET="$WEBHOOK_SECRET" \
  -e SSH_PRIVATE_KEY="$SSH_PRIVATE_KEY" \
  -v "$PWD/git_connectors.toml:/data/git_connectors.toml:ro" \
  docker.system3.md/relay-git-sync:latest \
  --data-dir /data
```

To generate auth from the container, omit `RELAY_SERVER_API_KEY`. The container reads
`git_connectors.toml`, prints the setup output, and exits:

```bash
docker run --rm \
  -v "$PWD/git_connectors.toml:/data/git_connectors.toml:ro" \
  docker.system3.md/relay-git-sync:latest \
  --data-dir /data
```

#### Command Line Options

- `--port`: HTTP server port (default: 8000)
- `--commit-interval`: Git commit interval in seconds (default: 10)
- `--relay-server-url`: Relay server URL override
- `--relay-id`: Relay UUID override
- `-c, --config`: Path to `git_connectors.toml` when it is not `<data-dir>/git_connectors.toml`
- `--data-dir`: Data storage directory (default: from `RELAY_GIT_DATA_DIR` env var or current directory)
- `--webhook-secret`: Optional webhook secret (or set `WEBHOOK_SECRET`); enables `/webhooks`
- `--websocket-reconnect-delay`: Seconds to wait before reconnecting Relay websocket listeners (default: 5)

#### Push Failure Logging

While a repo's pushes are failing, git-sync retries at most once a minute, from the commit
timer or with the next commit, until one succeeds. Each failure is logged with the remote's
reason. Once the oldest commit no remote has accepted is
older than `RELAY_GIT_STALLED_PUSH_WARNING_SECONDS` (default: 300), every failed attempt also
logs a warning, and once it is older than `RELAY_GIT_STALLED_PUSH_ERROR_SECONDS` (default: 1800),
an error instead. Alerting can key on either line:

```
Push stalled for <relay_id>/<folder_id>: N commits unpushed, oldest from M minutes ago
Push stalled over 30 minutes for <relay_id>/<folder_id>: N commits unpushed, oldest from M minutes ago
```


### Manual Sync

You can also perform one-time syncs within your container by using the CLI:

```bash
export RELAY_SERVER_API_KEY=your-server-api-key
uv run git-sync sync --folder-id <folder-uuid>
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
        ├── subdoc_heads.json     # Persisted Relay subdocument index heads
        ├── shared_folders.json   # Folder metadata
        └── local_state.json      # File state per folder
```

## API Documentation

The server provides public API endpoints for management. The webhook endpoint is only mounted when `WEBHOOK_SECRET` is configured.

**Interactive Documentation:** Visit `/docs` when the server is running for interactive Swagger UI documentation.

**OpenAPI Specification:** See [`openapi.yaml`](./openapi.yaml) for complete API documentation with request/response schemas, authentication details, and examples.

**Key Endpoints:**
- `GET /health` - Health check (public)  
- `GET /api/pubkey` - SSH public key retrieval (public)
- `GET /docs` - Interactive API documentation (public)
- `GET /openapi.yaml` - OpenAPI specification (public)
- `POST /webhooks` - Optional webhook ingestion, mounted only when `WEBHOOK_SECRET` is set
