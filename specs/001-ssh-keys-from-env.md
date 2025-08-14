# Architecture Change Request: Environment Variable SSH Keys

## Overview
Remove file-based SSH key management and require SSH private keys to be provided via environment variable. This supports stateless deployments without persistent volumes.

## Current State

### SSH Key Management Architecture
The current system uses the `SSHKeyManager` class in `persistence.py` which:

1. **File-based Storage**: Stores SSH keys in `{data_dir}/ssh/git_sync_key` and `{data_dir}/ssh/git_sync_key.pub`
2. **Auto-generation**: Automatically generates 2048-bit RSA key pairs if they don't exist
3. **Git Integration**: Sets `GIT_SSH_COMMAND` environment variable to use the private key file
4. **CLI Access**: Provides commands to display public keys and regenerate key pairs

### Key Usage Points
- `SSHKeyManager.__init__()`: Sets up file paths based on data directory
- `SSHKeyManager.ensure_keys_exist()`: Generates keys if missing
- `SSHKeyManager.generate_keys()`: Creates new RSA key pair and writes to files
- `SSHKeyManager.get_public_key()`: Reads public key from file
- `PersistenceManager._safe_git_operation()`: Sets `GIT_SSH_COMMAND` with private key path

## Proposed Changes

### 1. Remove File-Based Key Management
Replace `SSHKeyManager` with environment-only approach:

```python
class SSHKeyManager:
    def __init__(self):
        self._temp_key_path = None
        self._setup_temp_key()
    
    def _setup_temp_key(self):
        """Create temporary key file from environment variable"""
        private_key_pem = os.getenv("SSH_PRIVATE_KEY")
        if not private_key_pem:
            raise ValueError("SSH_PRIVATE_KEY environment variable is required")
        
        self._temp_key_path = self._create_temp_key_file(private_key_pem)
```

### 2. Temporary Key File Management
Create secure temporary files from environment variable:

```python
def _create_temp_key_file(self, private_key_pem: str) -> str:
    """Create temporary file for private key from environment variable"""
    import tempfile
    fd, temp_path = tempfile.mkstemp(prefix='git_sync_key_', suffix='.pem')
    try:
        os.write(fd, private_key_pem.encode('utf-8'))
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
```

### 3. Public Key Extraction
Extract public key from private key:

```python
def get_public_key(self) -> str:
    """Extract public key from private key"""
    private_key_pem = os.getenv("SSH_PRIVATE_KEY")
    if not private_key_pem:
        raise ValueError("SSH_PRIVATE_KEY environment variable is required")
    
    # Load private key
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode('utf-8'),
        password=None,
        backend=default_backend()
    )
    
    # Extract public key
    public_key = private_key.public_key()
    public_ssh = public_key.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH
    )
    
    return public_ssh.decode('utf-8').strip()
```

### 4. Remove Key Generation
- Remove `generate_keys()` method entirely
- Remove `ensure_keys_exist()` method
- Remove SSH directory creation logic
- Remove key generation CLI commands

### 5. Update PersistenceManager
Remove `data_dir` parameter from `SSHKeyManager` initialization:

```python
class PersistenceManager:
    def __init__(self, data_dir: str = "."):
        self.data_dir = data_dir
        self.git_repos: Dict[str, git.Repo] = {}
        self.git_lock = threading.Lock()
        self.ssh_key_manager = SSHKeyManager()  # No data_dir parameter
```

### 6. CLI Updates
- Remove `ssh keygen` command
- Update `show-pubkey` to only extract from environment variable
- Add validation to ensure `SSH_PRIVATE_KEY` is set on startup

```python
def show_pubkey_command(args):
    """Handle show-pubkey command"""
    try:
        ssh_key_manager = SSHKeyManager()
        pubkey = ssh_key_manager.get_public_key()
        print("\nSSH Public Key (extracted from SSH_PRIVATE_KEY):")
        print("=" * 50)
        print(pubkey)
        print("=" * 50)
        print("\nAdd this key to your Git hosting service as a deploy key.")
        return 0
    except ValueError as e:
        print(f"Error: {e}")
        print("Set SSH_PRIVATE_KEY environment variable with your private key.")
        return 1
```

### 7. Public API Endpoint
Add a public HTTP endpoint to retrieve the SSH public key without authentication:

```python
@noauth
async def get_pubkey(self, request: Request):
    """Get SSH public key - no authentication required"""
    try:
        # Check if SSH key manager is available
        if not self.persistence_manager.ssh_key_manager:
            return JSONResponse(
                {"error": "SSH_PRIVATE_KEY environment variable not configured"}, 
                status_code=400
            )
        
        # Get the public key
        public_key = self.persistence_manager.ssh_key_manager.get_public_key()
        
        # Determine key type from the public key string
        key_type = "unknown"
        if public_key.startswith("ssh-rsa"):
            key_type = "ssh-rsa"
        elif public_key.startswith("ssh-ed25519"):
            key_type = "ssh-ed25519"
        elif public_key.startswith("ecdsa-sha2-"):
            key_type = "ecdsa"
        
        return JSONResponse({
            "public_key": public_key,
            "key_type": key_type
        })
        
    except Exception as e:
        logger.error(f"Error getting public key: {e}")
        return JSONResponse(
            {"error": f"Failed to retrieve public key: {str(e)}"}, 
            status_code=500
        )
```

**Endpoint**: `GET /api/pubkey`
**Authentication**: None required (public endpoint)
**Response Format**: JSON with `public_key` and `key_type` fields

## Implementation Plan

### Phase 1: Refactor SSHKeyManager
1. Remove all file-based logic
2. Implement temporary file creation from environment variable
3. Implement public key extraction
4. Add proper error handling for missing/invalid keys

### Phase 2: Update Dependencies
1. Update `PersistenceManager` to use new `SSHKeyManager`
2. Update `_safe_git_operation()` to use new key path
3. Remove all references to SSH directory

### Phase 3: CLI Cleanup
1. Remove `ssh keygen` command
2. Update `show-pubkey` command
3. Add startup validation for `SSH_PRIVATE_KEY`
4. Update help text and examples

### Phase 3.5: API Endpoint
1. Add `GET /api/pubkey` endpoint to web server
2. Update web server constructor to accept `persistence_manager`
3. Add comprehensive tests for the new endpoint
4. Update API documentation in README

### Phase 4: Testing & Documentation
1. Update all tests to set `SSH_PRIVATE_KEY`
2. Remove tests for key generation
3. Update README and deployment examples
4. Update error messages

## Security Considerations

### Environment Variable Security
- Private keys must be provided via secure mechanisms
- Temporary files created with 600 permissions
- Automatic cleanup on process exit
- No keys stored on disk

### Validation
- Validate PEM format on startup
- Ensure private key is valid RSA/Ed25519 key
- Clear error messages for invalid/missing keys

## Benefits

### Simplified Architecture
- No persistent volume requirements
- No SSH directory management
- No key generation logic
- Cleaner codebase

### Better Security
- Keys never touch persistent storage
- Forced use of external key management
- No accidental key exposure via volumes

### Operational Clarity
- Explicit key configuration required
- No hidden key generation
- Clear separation of concerns

### Public API Access
- HTTP endpoint for retrieving public key programmatically
- No authentication required for public key access
- Enables automated deployment and configuration workflows
- Supports CI/CD integration for Git hosting service configuration

## Migration Requirements

### For All Deployments
1. Generate SSH key pair externally
2. Set `SSH_PRIVATE_KEY` environment variable
3. Remove persistent volume mounts
4. Update deployment scripts

## Example Usage

### Key Generation (External)
```bash
# Generate new key pair
ssh-keygen -t rsa -b 2048 -f git_sync_key -N ""

# Or use ed25519 (recommended)
ssh-keygen -t ed25519 -f git_sync_key -N ""
```

### Fly.io Deployment
```bash
# Set the private key as a secret
fly secrets set SSH_PRIVATE_KEY="$(cat git_sync_key)"

# Deploy (no volumes needed)
fly deploy
```

### Docker Deployment
```bash
# Run with environment variable
docker run -e SSH_PRIVATE_KEY="$(cat git_sync_key)" git-sync
```

### Using the Public Key API
```bash
# Get the public key via HTTP API (no authentication required)
curl http://localhost:8000/api/pubkey

# Example response:
{
  "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyData",
  "key_type": "ssh-ed25519"
}

# Extract just the public key for automation
PUBLIC_KEY=$(curl -s http://localhost:8000/api/pubkey | jq -r .public_key)
echo "$PUBLIC_KEY"
```

### Kubernetes Deployment
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: git-sync-ssh
data:
  private-key: <base64-encoded-private-key>
---
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
      - name: git-sync
        env:
        - name: SSH_PRIVATE_KEY
          valueFrom:
            secretKeyRef:
              name: git-sync-ssh
              key: private-key
```

## Error Handling

### Missing Environment Variable
```
Error: SSH_PRIVATE_KEY environment variable is required
Please set SSH_PRIVATE_KEY with your PEM-encoded private key
```

### Invalid Key Format
```
Error: Invalid private key format in SSH_PRIVATE_KEY
Ensure the key is PEM-encoded (begins with -----BEGIN PRIVATE KEY-----)
```

### Key Type Issues
```
Error: Unsupported key type
Supported types: RSA, Ed25519
```