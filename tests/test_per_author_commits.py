import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import git

from attribution import AuthorResolver, GitAuthor
from persistence import PersistenceManager

RELAY_ID = "85a06712-af14-47bc-a859-e8106cc786e8"
FOLDER_ID = "3667fcda-755e-472b-abea-4b4fc96873a9"
REPO_KEY = f"{RELAY_ID}/{FOLDER_ID}"
PREFIX = "notes"

ADA = GitAuthor("Ada Lovelace", "ada@example.com")
BOB = GitAuthor("Bob Bobson", "bob@example.com")


def make_persistence(temp_dir):
    config_path = os.path.join(temp_dir, "git_connectors.toml")
    with open(config_path, "w") as f:
        f.write(
            f"""
[relay]
id = "{RELAY_ID}"
url = "https://auth.example.com"

[[git_connector]]
shared_folder_id = "{FOLDER_ID}"
branch = "main"
prefix = "{PREFIX}"

[authors]
user-ada = "{ADA.name} <{ADA.email}>"
user-bob = "{BOB.name} <{BOB.email}>"
"""
        )
    with patch.object(PersistenceManager, "_initialize_all_git_repos"):
        return PersistenceManager(temp_dir)


def make_repo(temp_dir):
    repo = git.Repo.init(os.path.join(temp_dir, "repo"), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Sync Bot")
        cw.set_value("user", "email", "bot@example.com")
    notes = os.path.join(repo.working_dir, PREFIX)
    os.makedirs(notes)
    with open(os.path.join(notes, "existing.md"), "w") as f:
        f.write("original line\n")
    repo.git.add(A=True)
    repo.index.commit("Initial commit")
    return repo


def write(repo, rel_path, content):
    path = os.path.join(repo.working_dir, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def test_commit_changes_groups_by_author():
    temp_dir = tempfile.mkdtemp()
    try:
        persistence = make_persistence(temp_dir)
        repo = make_repo(temp_dir)
        persistence.git_repos = {REPO_KEY: repo}
        persistence._push_to_remote = MagicMock()
        persistence._ensure_configured_branch = MagicMock()

        write(repo, f"{PREFIX}/existing.md", "original line\nada's addition\n")
        write(repo, f"{PREFIX}/new-note.md", "bob's brand new note\n")
        write(repo, f"{PREFIX}/unattributed.md", "no doc mapping for this one\n")
        persistence.local_file_state = {
            RELAY_ID: {
                FOLDER_ID: {
                    "/existing.md": {"doc_id": "doc-ada", "type": "markdown"},
                    "/new-note.md": {"doc_id": "doc-bob", "type": "markdown"},
                }
            }
        }

        spans_by_doc = {
            "doc-ada": [
                {"text": "original line\n", "client_id": 1, "user": "user-ada"},
                {"text": "ada's addition\n", "client_id": 2, "user": "user-ada"},
            ],
            "doc-bob": [{"text": "bob's brand new note\n", "client_id": 3, "user": "user-bob"}],
        }
        persistence.author_resolver = AuthorResolver(
            lambda resource: spans_by_doc.get(resource.document_id),
            {"user-ada": ADA, "user-bob": BOB},
        )

        assert persistence.commit_changes() is True

        commits = list(repo.iter_commits("main"))[:-1]  # drop the initial commit
        authors = {c.author.email: set(c.stats.files) for c in commits}
        assert authors[ADA.email] == {f"{PREFIX}/existing.md"}
        assert authors[BOB.email] == {f"{PREFIX}/new-note.md"}
        assert authors["bot@example.com"] == {f"{PREFIX}/unattributed.md"}
        assert not repo.is_dirty(untracked_files=True)
    finally:
        shutil.rmtree(temp_dir)


def test_rename_deletion_joins_the_authors_commit():
    temp_dir = tempfile.mkdtemp()
    try:
        persistence = make_persistence(temp_dir)
        repo = make_repo(temp_dir)
        persistence.git_repos = {REPO_KEY: repo}
        persistence._push_to_remote = MagicMock()
        persistence._ensure_configured_branch = MagicMock()

        os.remove(os.path.join(repo.working_dir, PREFIX, "existing.md"))
        write(repo, f"{PREFIX}/renamed.md", "original line\n")
        persistence.local_file_state = {
            RELAY_ID: {
                FOLDER_ID: {"/renamed.md": {"doc_id": "doc-ada", "type": "markdown"}}
            }
        }
        persistence.author_resolver = AuthorResolver(
            lambda resource: [{"text": "original line\n", "client_id": 1, "user": "user-ada"}],
            {"user-ada": ADA},
        )

        assert persistence.commit_changes() is True

        commits = list(repo.iter_commits("main"))
        assert len(commits) == 2  # initial + one attributed commit, no bot sweep
        assert commits[0].author.email == ADA.email
        assert "R100" in repo.git.diff("HEAD~1", "HEAD", "-M", "--name-status")
        assert not repo.is_dirty(untracked_files=True)
    finally:
        shutil.rmtree(temp_dir)


def test_shared_file_gets_co_author_trailer():
    temp_dir = tempfile.mkdtemp()
    try:
        persistence = make_persistence(temp_dir)
        repo = make_repo(temp_dir)
        persistence.git_repos = {REPO_KEY: repo}
        persistence._push_to_remote = MagicMock()
        persistence._ensure_configured_branch = MagicMock()

        write(repo, f"{PREFIX}/existing.md", "original line\nada wrote most of this\nbob bit\n")
        persistence.local_file_state = {
            RELAY_ID: {
                FOLDER_ID: {"/existing.md": {"doc_id": "doc-shared", "type": "markdown"}}
            }
        }
        persistence.author_resolver = AuthorResolver(
            lambda resource: [
                {"text": "original line\n", "client_id": 1, "user": "user-ada"},
                {"text": "ada wrote most of this\n", "client_id": 2, "user": "user-ada"},
                {"text": "bob bit\n", "client_id": 3, "user": "user-bob"},
            ],
            {"user-ada": ADA, "user-bob": BOB},
        )

        assert persistence.commit_changes() is True

        commits = list(repo.iter_commits("main"))
        assert len(commits) == 2
        assert commits[0].author.email == ADA.email
        assert f"Co-authored-by: {BOB.name} <{BOB.email}>" in commits[0].message
        assert not repo.is_dirty(untracked_files=True)
    finally:
        shutil.rmtree(temp_dir)


def test_rename_split_across_cycles_still_commits_atomically():
    """Relay sends a rename as delete + create events, sometimes in different
    commit cycles. The unpaired deletion must be deferred, not committed."""
    temp_dir = tempfile.mkdtemp()
    try:
        persistence = make_persistence(temp_dir)
        repo = make_repo(temp_dir)
        persistence.git_repos = {REPO_KEY: repo}
        persistence._push_to_remote = MagicMock()
        persistence._ensure_configured_branch = MagicMock()
        persistence._pull_from_remote = MagicMock()
        persistence.local_file_state = {RELAY_ID: {FOLDER_ID: {}}}
        persistence.author_resolver = AuthorResolver(
            lambda resource: [{"text": "original line\n", "client_id": 1, "user": "user-ada"}],
            {"user-ada": ADA},
        )

        # cycle 1: only the delete half has arrived
        os.remove(os.path.join(repo.working_dir, PREFIX, "existing.md"))
        assert persistence.commit_changes() is False  # deferred, nothing committed
        assert len(list(repo.iter_commits("main"))) == 1
        assert repo.head.commit.tree[f"{PREFIX}/existing.md"]  # deletion not yet committed

        # cycle 2: the create half arrives
        write(repo, f"{PREFIX}/renamed.md", "original line\n")
        persistence.local_file_state = {
            RELAY_ID: {FOLDER_ID: {"/renamed.md": {"doc_id": "doc-ada", "type": "markdown"}}}
        }
        assert persistence.commit_changes() is True

        commits = list(repo.iter_commits("main"))
        assert len(commits) == 2
        assert commits[0].author.email == ADA.email
        assert "R100" in repo.git.diff("HEAD~1", "HEAD", "-M", "--name-status")
        assert not repo.is_dirty(untracked_files=True)
    finally:
        shutil.rmtree(temp_dir)


def test_unpaired_deletion_released_after_window(monkeypatch):
    import persistence as persistence_module

    monkeypatch.setattr(persistence_module, "DELETION_PAIRING_WINDOW_S", 0.0)
    temp_dir = tempfile.mkdtemp()
    try:
        persistence = make_persistence(temp_dir)
        repo = make_repo(temp_dir)
        persistence.git_repos = {REPO_KEY: repo}
        persistence._push_to_remote = MagicMock()
        persistence._ensure_configured_branch = MagicMock()
        persistence._pull_from_remote = MagicMock()
        persistence.local_file_state = {RELAY_ID: {FOLDER_ID: {}}}
        persistence.author_resolver = AuthorResolver(
            lambda resource: [], {"user-ada": ADA}
        )

        os.remove(os.path.join(repo.working_dir, PREFIX, "existing.md"))
        assert persistence.commit_changes() is True  # window expired: bot commits it

        commits = list(repo.iter_commits("main"))
        assert len(commits) == 2
        assert commits[0].author.email == "bot@example.com"
        assert not repo.is_dirty(untracked_files=True)
    finally:
        shutil.rmtree(temp_dir)


def test_commit_changes_without_resolver_is_single_commit():
    temp_dir = tempfile.mkdtemp()
    try:
        persistence = make_persistence(temp_dir)
        repo = make_repo(temp_dir)
        persistence.git_repos = {REPO_KEY: repo}
        persistence._push_to_remote = MagicMock()
        persistence._ensure_configured_branch = MagicMock()
        persistence.author_resolver = None

        write(repo, f"{PREFIX}/existing.md", "changed\n")
        write(repo, f"{PREFIX}/other.md", "more\n")

        assert persistence.commit_changes() is True

        commits = list(repo.iter_commits("main"))
        assert len(commits) == 2  # initial + one auto-sync
        assert commits[0].author.email == "bot@example.com"
        assert not repo.is_dirty(untracked_files=True)
    finally:
        shutil.rmtree(temp_dir)


def test_deferred_deletion_sets_pending_flag_until_released(monkeypatch):
    """A commit of unrelated work must not strand a young unpaired deletion:
    deferred_deletions_pending stays True until the deferral is released and
    committed, so the commit timer keeps calling commit_changes."""
    import persistence as persistence_module

    temp_dir = tempfile.mkdtemp()
    try:
        persistence = make_persistence(temp_dir)
        repo = make_repo(temp_dir)
        persistence.git_repos = {REPO_KEY: repo}
        persistence._push_to_remote = MagicMock()
        persistence._ensure_configured_branch = MagicMock()
        persistence._pull_from_remote = MagicMock()
        persistence.local_file_state = {RELAY_ID: {FOLDER_ID: {}}}
        persistence.author_resolver = AuthorResolver(lambda resource: [], {"user-ada": ADA})

        # a deletion arrives alongside unrelated new work
        os.remove(os.path.join(repo.working_dir, PREFIX, "existing.md"))
        write(repo, f"{PREFIX}/unrelated.md", "new note\n")

        # cycle 1: unrelated work commits, deletion is young -> deferred
        assert persistence.commit_changes() is True
        assert persistence.deferred_deletions_pending is True
        assert repo.head.commit.tree[f"{PREFIX}/existing.md"]  # deletion held back

        # cycle 2 (window expired): released to the bot commit, flag clears
        monkeypatch.setattr(persistence_module, "DELETION_PAIRING_WINDOW_S", 0.0)
        assert persistence.commit_changes() is True
        assert persistence.deferred_deletions_pending is False
        assert f"{PREFIX}/existing.md" not in repo.head.commit.tree
        assert not repo.is_dirty(untracked_files=True)
    finally:
        shutil.rmtree(temp_dir)
