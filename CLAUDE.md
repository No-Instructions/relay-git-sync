# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a real-time synchronization bridge between Relay Server collaborative documents and Git repositories. The system monitors collaborative documents via webhooks and maintains synchronized copies in local Git repositories with automatic version control.

## Key Architecture

The codebase follows a modular architecture with clear separation of concerns:

### Core Components

- **RelayClient** (`relay_client.py`): Wrapper around Y-Sweet DocumentManager with authentication handling
- **SyncEngine** (`sync_engine.py`): Core synchronization logic for converting Y-Sweet documents to Git repositories
- **OperationsQueue** (`operations_queue.py`): Thread-safe queue for processing sync requests with git commit coordination
- **WebhookProcessor** (`webhook_handler.py`): Processes incoming webhook notifications from Relay Server
- **WebServer** (`web_server.py`): HTTP server that handles webhook endpoints
- **PersistenceManager** (`persistence.py`): Manages persistent state files and Git repository operations

### Data Models

- **SyncOperation** (`models.py`): Data structure representing file operations (CREATE, UPDATE, RENAME, DELETE)
- **SyncRequest/SyncResult** (`models.py`): Request/response structures for sync operations
- **S3RN Resources** (`s3rn.py`): Resource naming system for Relay Server resources (folders, documents, files, canvas)

### Synchronization Flow

1. **Webhook Reception** (`web_server.py`): HTTP endpoint receives document change notifications
2. **Queue Management** (`operations_queue.py`): Requests are queued and processed by worker threads
3. **Document Analysis** (`sync_engine.py`): Determines resource type (folder vs document vs file vs canvas)
4. **Sync Operations** (`sync_engine.py`): Three-phase sync process with conflict resolution
5. **Git Commits** (`persistence.py`): Automatic commits every 10 seconds when changes detected

### Data Storage

- **State Directory**: `state/<relay_id>/` contains persistent state per relay:
  - `document_hashes.json`: Hash tracking for change detection
  - `shared_folders.json`: Folder metadata from Relay Server
  - `local_state.json`: Local file tracking per folder
- **Repository Directory**: `repos/<relay_id>/<folder_id>/` contains synchronized content organized by relay and folder

## Development Commands

### Running the Server
```bash
# Run webhook server (default port 8000)
python app.py

# Run with custom configuration
python app.py --port 8080 --commit-interval 30 --relay-server-url "http://localhost:8080" --data-dir "/custom/path"
```

### Dependencies
```bash
# Install dependencies
uv sync

# Install dev dependencies for linting/formatting
uv sync --group dev
```

### Code Quality
```bash
# Format code
black .
isort .

# Run pre-commit hooks
pre-commit run --all-files
```

### Deployment
The project includes Fly.io configuration (`fly.toml`) for deployment with persistent storage mounted at `/data`.

## Key Implementation Details

### Multi-Relay/Multi-Folder Support
- Each relay gets its own state directory and repository structure
- Per-relay and per-folder sync locks prevent concurrent operations
- Hierarchical organization: `repos/<relay_id>/<folder_id>/`

### Resource Type Handling
- **Folders**: Contain `filemeta_v0` Map with file metadata
- **Documents**: Contain `contents` Text with document content  
- **Files**: Binary content with metadata
- **Canvas**: Collaborative whiteboard content

### Conflict Resolution
- Hash-based change detection prevents unnecessary updates
- Rename detection by tracking document IDs across path changes
- Thread-safe operations with per-folder locking

### Authentication
- Supports API key authentication via relay server URL
- SSH key management for Git operations (`persistence.py`)

## File Structure

```
.
├── app.py                 # Main entry point and server orchestration
├── models.py              # Data structures and type definitions
├── relay_client.py        # Y-Sweet DocumentManager wrapper
├── sync_engine.py         # Core synchronization logic
├── operations_queue.py    # Thread-safe request processing
├── webhook_handler.py     # Webhook processing logic
├── web_server.py          # HTTP server implementation
├── persistence.py         # State management and Git operations
├── s3rn.py               # Resource naming and type system
├── repos/                # Synchronized Git repositories
├── state/                # Persistent state files per relay
└── y_sweet_sdk/          # Y-Sweet SDK for document operations
```

## Testing

- Run the tests with `uv run pytest`