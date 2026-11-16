#!/bin/bash

# SSH is fully managed by the app: the key from SSH_PRIVATE_KEY is written to
# a key file and GIT_SSH_COMMAND enforces it with strict host key checking
# against known_hosts from git_connectors.toml and hosted provider keys.

exec uv run --frozen --no-sync app.py "$@"
