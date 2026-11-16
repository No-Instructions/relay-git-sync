Relay Git Sync for Linux

Extract the entire archive and keep the _internal directory beside the executables.
Python and uv are bundled or unnecessary; no Python installation is required.
Install Git and OpenSSH client using your distribution's package manager.
Requires glibc 2.36 or newer (for example, Debian 12 or Ubuntu 24.04).

Use ./git-sync --help for configuration, authentication setup, and manual sync.
Use ./git-sync-server --help for the long-running sync service.
Both executables accept --data-dir to select your persistent configuration/data.
Keep credentials in environment variables, as described in the project README.

Project documentation:
https://github.com/No-Instructions/relay-git-sync#readme

Build from the repository root:
docker buildx build --platform linux/amd64,linux/arm64 -f Dockerfile.release --output type=local,dest=dist .
