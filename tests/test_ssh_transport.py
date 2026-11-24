"""End-to-end SSH transport checks for the shipped OpenSSH client (CVE-2026-60002).

Git Sync reaches SSH remotes through the ``ssh`` program on PATH, configured by
``PersistenceManager`` with strict host-key checking and the operator's known_hosts.
These tests run a real ``sshd`` on loopback and drive clone, fetch, commit and push
through the application's own code path, with rekeying forced during the transfer,
then confirm that a changed server host key is rejected. They are skipped when no
``sshd`` is installed; the container test stage installs one from the same OpenSSH
build as the shipped client.
"""

import os
import pwd
import shutil
import socket
import subprocess
import time
from unittest.mock import patch

import git
import pytest

from persistence import PersistenceManager

SBIN_PATH = os.environ.get("PATH", "") + os.pathsep + "/usr/local/sbin:/usr/sbin:/sbin"
SSHD = shutil.which("sshd", path=SBIN_PATH)
SSH = shutil.which("ssh")
SSH_KEYGEN = shutil.which("ssh-keygen")
RELAY_ID = "85a06712-af14-47bc-a859-e8106cc786e8"
FOLDER_ID = "3667fcda-755e-472b-abea-4b4fc96873a9"
OTHER_FOLDER_ID = "6d3f0c2e-1b7a-4c8e-9f21-0a5b6c7d8e9f"
# Force several key re-exchanges while cloning a 1 MiB payload.
REKEY_LIMIT = "64K"
PAYLOAD_BYTES = 1024 * 1024

pytestmark = pytest.mark.skipif(
    not (SSHD and SSH and SSH_KEYGEN),
    reason="requires sshd, ssh and ssh-keygen on PATH",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


class LoopbackSshd:
    """A loopback sshd serving the current user with publickey authentication only."""

    def __init__(self, root: str, port: int, authorized_key: str):
        self.root = root
        self.port = port
        self.hostkey = os.path.join(root, "hostkey")
        self.log = os.path.join(root, "sshd.log")
        self.config = os.path.join(root, "sshd_config")
        self.proc = None
        _run(SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", self.hostkey)
        with open(os.path.join(root, "authorized_keys"), "w", encoding="utf-8") as f:
            f.write(authorized_key + "\n")
        git_dir = os.path.dirname(shutil.which("git"))
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(
                "\n".join(
                    [
                        f"Port {port}",
                        "ListenAddress 127.0.0.1",
                        f"HostKey {self.hostkey}",
                        f"AuthorizedKeysFile {root}/authorized_keys",
                        "PasswordAuthentication no",
                        "KbdInteractiveAuthentication no",
                        "StrictModes no",
                        "PidFile none",
                        "LogLevel DEBUG1",
                        f"RekeyLimit {REKEY_LIMIT}",
                        f"SetEnv PATH={git_dir}:/usr/local/bin:/usr/bin:/bin",
                        "",
                    ]
                )
            )

    def known_hosts_entry(self) -> str:
        with open(self.hostkey + ".pub", encoding="utf-8") as f:
            key_type, key_blob = f.read().split()[:2]
        return f"[127.0.0.1]:{self.port} {key_type} {key_blob}"

    def start(self) -> None:
        self.proc = subprocess.Popen([SSHD, "-D", "-f", self.config, "-E", self.log])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"sshd exited early:\n{self.read_log()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"sshd did not listen on {self.port}:\n{self.read_log()}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def read_log(self) -> str:
        try:
            with open(self.log, encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""


@pytest.fixture
def ssh_env(tmp_path):
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def transport(tmp_path, ssh_env):
    """Origin repository, loopback sshd and a PersistenceManager wired to it over SSH."""
    client_key = tmp_path / "client_key"
    _run(SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(client_key))
    authorized_key = (tmp_path / "client_key.pub").read_text(encoding="utf-8").strip()

    origin = tmp_path / "origin.git"
    git.Repo.init(str(origin), bare=True, initial_branch="main")
    seed_dir = tmp_path / "seed"
    seed = git.Repo.init(str(seed_dir), initial_branch="main")
    with seed.config_writer() as cw:
        cw.set_value("user", "name", "seed")
        cw.set_value("user", "email", "seed@example.com")
    (seed_dir / "payload.bin").write_bytes(os.urandom(PAYLOAD_BYTES))
    (seed_dir / "README.md").write_text("seed\n", encoding="utf-8")
    seed.git.add("-A")
    seed.index.commit("seed", skip_hooks=True)
    seed.git.push(str(origin), "main:main")

    server_root = tmp_path / "sshd"
    server_root.mkdir()
    sshd = LoopbackSshd(str(server_root), _free_port(), authorized_key)
    sshd.start()

    user = pwd.getpwuid(os.getuid()).pw_name
    url = f"ssh://{user}@127.0.0.1:{sshd.port}{origin}"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_file = data_dir / "git_connectors.toml"
    config_file.write_text(
        f'known_hosts = ["{sshd.known_hosts_entry()}"]\n\n'
        f'[relay]\nid = "{RELAY_ID}"\nurl = "https://relay.example.invalid"\n\n'
        f'[[git_connector]]\nshared_folder_id = "{FOLDER_ID}"\nurl = "{url}"\nbranch = "main"\n\n'
        f'[[git_connector]]\nshared_folder_id = "{OTHER_FOLDER_ID}"\nurl = "{url}"\nbranch = "main"\n',
        encoding="utf-8",
    )
    for key in list(os.environ):
        if key.startswith("GIT_CONFIG") or key in ("GIT_SSH_COMMAND", "GIT_SSH"):
            del os.environ[key]
    os.environ["SSH_PRIVATE_KEY"] = client_key.read_text(encoding="utf-8")
    with patch.object(PersistenceManager, "_initialize_all_git_repos"):
        persistence = PersistenceManager(str(data_dir), str(config_file))
    try:
        yield persistence, sshd, str(origin), str(server_root)
    finally:
        sshd.stop()


def _configure_identity(repo: git.Repo) -> None:
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "git-sync")
        cw.set_value("user", "email", "git-sync@example.com")


def test_ssh_command_uses_strict_host_key_checking(transport):
    persistence, sshd, _origin, _root = transport
    command = os.environ["GIT_SSH_COMMAND"]
    assert command.startswith("ssh ")
    assert "-o StrictHostKeyChecking=yes" in command
    assert f"-o UserKnownHostsFile={persistence.ssh_key_manager.known_hosts_path}" in command
    with open(persistence.ssh_key_manager.known_hosts_path, encoding="utf-8") as f:
        assert f.read().strip() == sshd.known_hosts_entry()


def test_clone_fetch_commit_push_over_ssh_with_rekeying(transport):
    persistence, sshd, origin, _root = transport

    repo = persistence.init_git_repo(RELAY_ID, FOLDER_ID)
    assert [r.name for r in repo.remotes] == ["origin"], "clone over SSH must succeed"
    assert os.path.getsize(os.path.join(repo.working_tree_dir, "payload.bin")) == PAYLOAD_BYTES
    # The 1 MiB payload crossed the server's RekeyLimit, so the connection re-keyed
    # (and the client re-verified the host key) after authentication: sshd logs the
    # post-auth key change as "ssh_set_newkeys: rekeying" without a [preauth] tag.
    rekeys = [
        line
        for line in sshd.read_log().splitlines()
        if "ssh_set_newkeys: rekeying" in line and "[preauth]" not in line
    ]
    assert rekeys, sshd.read_log()

    repo.git.fetch("origin")

    _configure_identity(repo)
    with open(os.path.join(repo.working_tree_dir, "note.md"), "w", encoding="utf-8") as f:
        f.write("synced over ssh\n")
    assert persistence.commit_changes() is True
    pushed = git.Repo(origin).commit("main").hexsha
    assert pushed == repo.head.commit.hexsha, "commit must be pushed to the SSH remote"


def test_changed_host_key_is_rejected(transport):
    persistence, sshd, origin, server_root = transport
    repo = persistence.init_git_repo(RELAY_ID, FOLDER_ID)
    assert [r.name for r in repo.remotes] == ["origin"]
    before = repo.head.commit.hexsha

    # Replace the server with one presenting a different host key on the same port.
    sshd.stop()
    impostor_root = os.path.join(server_root, "impostor")
    os.makedirs(impostor_root)
    with open(os.path.join(server_root, "authorized_keys"), encoding="utf-8") as f:
        authorized_key = f.read().strip()
    impostor = LoopbackSshd(impostor_root, sshd.port, authorized_key)
    assert impostor.known_hosts_entry() != sshd.known_hosts_entry()
    impostor.start()
    try:
        with pytest.raises(git.GitCommandError) as excinfo:
            repo.git.fetch("origin")
        stderr = str(excinfo.value)
        assert "Host key verification failed" in stderr or "IDENTIFICATION HAS CHANGED" in stderr
        assert repo.head.commit.hexsha == before

        # A fresh clone against the impostor must fail too; the application then
        # falls back to a local repository without a remote instead of trusting it.
        fallback = persistence.init_git_repo(RELAY_ID, OTHER_FOLDER_ID)
        assert not fallback.remotes
        assert not os.path.exists(os.path.join(fallback.working_tree_dir, "payload.bin"))
        assert "Accepted publickey" not in impostor.read_log(), impostor.read_log()
    finally:
        impostor.stop()
