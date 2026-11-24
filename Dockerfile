# Debian trixie ships git 2.47.3, which is affected by CVE-2026-111760 (index path
# length truncation lets a crafted remote write into .git during checkout; fixed in
# git 2.50.2). Until Debian publishes a fixed package, build the fixed upstream
# release and install it over the distribution binaries.
ARG GIT_VERSION=2.50.2
ARG GIT_ARCHIVE_SHA256=923c9c29443917925a1eb2afc16ba5bc93a57f0880007bcc345b6bb5785e148c

FROM python:3.14-slim-trixie AS gitbuild
ARG GIT_VERSION
ARG GIT_ARCHIVE_SHA256
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gcc make libc6-dev \
    libcurl4-gnutls-dev libexpat1-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL -o git.tar.gz "https://codeload.github.com/git/git/tar.gz/refs/tags/v${GIT_VERSION}" \
    && echo "${GIT_ARCHIVE_SHA256}  git.tar.gz" | sha256sum -c - \
    && tar xzf git.tar.gz \
    && make -C "git-${GIT_VERSION}" -j"$(nproc)" prefix=/usr/local \
        NO_TCLTK=1 NO_GETTEXT=1 NO_PERL=1 NO_PYTHON=1 NO_OPENSSL=1 \
        NO_INSTALL_HARDLINKS=1 install \
    && /usr/local/bin/git --version

FROM python:3.14-slim-trixie AS runtime

# Install system dependencies. git itself comes from the gitbuild stage; only its
# runtime libraries are installed here so the Debian git/git-man packages stay out.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    ca-certificates \
    libcurl3t64-gnutls \
    libexpat1 \
    zlib1g \
    openssh-client \
    && sed -i '\|^mozilla/emSign_ECC_Root_CA_-_G3.crt$|d' /etc/ca-certificates.conf \
    && rm -f /usr/share/ca-certificates/mozilla/emSign_ECC_Root_CA_-_G3.crt \
    && update-ca-certificates --fresh \
    && rm -rf /var/lib/apt/lists/*

# Fixed git from the build stage (binaries, git-core helpers and templates).
COPY --from=gitbuild /usr/local/bin/git* /usr/local/bin/
COPY --from=gitbuild /usr/local/libexec/git-core /usr/local/libexec/git-core
COPY --from=gitbuild /usr/local/share/git-core /usr/local/share/git-core
ARG GIT_VERSION
RUN test "$(git --version)" = "git version ${GIT_VERSION}" && test "$(command -v git)" = /usr/local/bin/git

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

FROM runtime AS test
COPY tests ./tests
COPY pytest.ini ./
RUN uv sync --frozen && .venv/bin/python -m pytest -q

FROM runtime
