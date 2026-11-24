# Debian trixie ships git 2.47.3, which is affected by CVE-2026-111760 (index path
# length truncation lets a crafted remote write into .git during checkout; fixed in
# git 2.50.2). Until Debian publishes a fixed package, build the fixed upstream
# release and install it over the distribution binaries.
ARG GIT_VERSION=2.50.2
ARG GIT_ARCHIVE_SHA256=923c9c29443917925a1eb2afc16ba5bc93a57f0880007bcc345b6bb5785e148c

# Debian trixie ships openssh-client 10.0p1, which is affected by CVE-2026-60002 (the
# client's host-key verification callback kept pointers to freed connection state and
# could be used after free when a server presented a changed host key during rekeying;
# fixed in OpenSSH 10.4). Debian rates it no-dsa, so build the current upstream
# release and ship only its client programs instead of the distribution package.
ARG OPENSSH_VERSION=10.6p2
ARG OPENSSH_TAG=V_10_6_P2
ARG OPENSSH_ARCHIVE_SHA256=c93873149687f9c80226dced4a4588ad7d0ad1bcd0b3220b0bb155ac836e8719

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

FROM python:3.14-slim-trixie AS sshbuild
ARG OPENSSH_VERSION
ARG OPENSSH_TAG
ARG OPENSSH_ARCHIVE_SHA256
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gcc make libc6-dev libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# The release tag archive carries a generated configure script, so no autotools are
# needed. Only the client is shipped; sshd stays in this stage for the test target.
RUN curl -fsSL -o openssh.tar.gz "https://codeload.github.com/openssh/openssh-portable/tar.gz/refs/tags/${OPENSSH_TAG}" \
    && echo "${OPENSSH_ARCHIVE_SHA256}  openssh.tar.gz" | sha256sum -c - \
    && tar xzf openssh.tar.gz \
    && cd "openssh-portable-${OPENSSH_TAG}" \
    && ./configure --prefix=/usr/local --sysconfdir=/etc/ssh --without-pam \
    && make -j"$(nproc)" \
    && make install-nokeys \
    && test "$(/usr/local/bin/ssh -V 2>&1 | cut -d, -f1)" = "OpenSSH_${OPENSSH_VERSION}"

FROM python:3.14-slim-trixie AS runtime

# Install system dependencies. git and ssh come from the build stages; only their
# runtime libraries are installed here so the Debian git/git-man/openssh-client
# packages stay out.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    ca-certificates \
    libcurl3t64-gnutls \
    libexpat1 \
    libssl3t64 \
    zlib1g \
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

# Fixed OpenSSH client programs from the build stage. GIT_SSH_COMMAND invokes "ssh"
# from PATH, where /usr/local/bin precedes /usr/bin.
COPY --from=sshbuild /usr/local/bin/ssh /usr/local/bin/ssh-add /usr/local/bin/ssh-agent \
    /usr/local/bin/ssh-keygen /usr/local/bin/ssh-keyscan /usr/local/bin/scp /usr/local/bin/sftp /usr/local/bin/
COPY --from=sshbuild /usr/local/libexec/ssh-pkcs11-helper /usr/local/libexec/ssh-sk-helper /usr/local/libexec/
ARG OPENSSH_VERSION
RUN test "$(ssh -V 2>&1 | cut -d, -f1)" = "OpenSSH_${OPENSSH_VERSION}" \
    && test "$(command -v ssh)" = /usr/local/bin/ssh \
    && ! command -v /usr/bin/ssh

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
# The SSH transport tests run a real sshd from the same OpenSSH build against the
# shipped client; none of this reaches the final image.
COPY --from=sshbuild /usr/local/sbin/sshd /usr/local/sbin/sshd
COPY --from=sshbuild /usr/local/libexec/sshd-session /usr/local/libexec/sshd-auth /usr/local/libexec/
RUN mkdir -p /var/empty && useradd --system --no-create-home --home-dir /var/empty --shell /bin/false sshd
COPY tests ./tests
COPY pytest.ini ./
RUN uv sync --frozen && .venv/bin/python -m pytest -q

FROM runtime
