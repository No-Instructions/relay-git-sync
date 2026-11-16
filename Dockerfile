FROM python:3.14-slim-trixie

# Install system dependencies
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    openssh-client \
    && sed -i '\|^mozilla/emSign_ECC_Root_CA_-_G3.crt$|d' /etc/ca-certificates.conf \
    && rm -f /usr/share/ca-certificates/mozilla/emSign_ECC_Root_CA_-_G3.crt \
    && update-ca-certificates --fresh \
    && rm -rf /var/lib/apt/lists/*

# Both tools vendor CA bundles. Remove Python's obsolete pip bootstrap bundle;
# uv creates application environments without it.
RUN pip install --no-cache-dir --upgrade pip==26.3 uv==0.13.8 \
    && rm -rf /usr/local/lib/python*/ensurepip

# Set working directory
WORKDIR /app

# Copy dependency files and package metadata
COPY pyproject.toml uv.lock README.md ./

# Install Python dependencies before copying the local project.
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY *.py ./
COPY git_host_keys ./git_host_keys
COPY relay_sdk ./relay_sdk
COPY templates ./templates
COPY run.sh ./
RUN chmod +x run.sh

# Install the local console command, then remove the bootstrap package manager.
RUN uv sync --frozen --no-dev && python -m pip uninstall --yes pip

# Create data directory for persistent storage
RUN mkdir -p /data

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Expose port
EXPOSE 8000

# Run the application. Docker command arguments are passed to app.py.
ENTRYPOINT ["./run.sh"]
