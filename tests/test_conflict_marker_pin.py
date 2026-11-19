"""Regression tests for the CVE-2026-105437 mitigation.

git before 2.49.1 overflows its merge-size counter when a committed .gitattributes
sets a huge ``conflict-marker-size`` and a merge or rebase produces many conflicts.
Git Sync merges automatically, so every managed repository must pin the marker size
in ``$GIT_DIR/info/attributes``, which takes precedence over in-tree attributes.
"""

import os
import shutil
import tempfile
from unittest.mock import patch

import git
import pytest

from persistence import PersistenceManager

PIN = PersistenceManager.CONFLICT_MARKER_ATTRIBUTE
# Large enough to be distinguishable from the default, small enough to be harmless
# on unpatched git binaries.
IN_TREE_ATTRIBUTE = "* conflict-marker-size=40\n"


def _git_dir(repo: git.Repo) -> str:
    return repo.git_dir


def _attributes_path(repo: git.Repo) -> str:
    return os.path.join(_git_dir(repo), "info", "attributes")


def _check_attr(repo: git.Repo, path: str) -> str:
    # "<path>: conflict-marker-size: <value>"
    return repo.git.check_attr("conflict-marker-size", "--", path).rsplit(":", 1)[1].strip()


def _commit_all(repo: git.Repo, message: str) -> None:
    repo.git.add(A=True)
    repo.index.commit(message)


def _configure_identity(repo: git.Repo) -> None:
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "test")
        cw.set_value("user", "email", "test@example.com")


class TestConflictMarkerPin:
    relay_id = "85a06712-af14-47bc-a859-e8106cc786e8"
    folder_id = "3667fcda-755e-472b-abea-4b4fc96873a9"

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        with patch.object(PersistenceManager, "_initialize_all_git_repos"):
            self.persistence = PersistenceManager(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_new_repository_is_pinned(self):
        repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)

        with open(_attributes_path(repo), encoding="utf-8") as f:
            assert PIN in f.read().splitlines()
        assert _check_attr(repo, "note.md") == "7"

    def test_existing_repository_is_pinned_when_opened(self):
        folder_path = self.persistence.get_folder_path(self.relay_id, self.folder_id)
        os.makedirs(folder_path, exist_ok=True)
        existing = git.Repo.init(folder_path, initial_branch="main")
        _configure_identity(existing)
        with open(os.path.join(folder_path, "note.md"), "w", encoding="utf-8") as f:
            f.write("hello\n")
        _commit_all(existing, "seed")
        assert not os.path.exists(_attributes_path(existing))

        repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)

        assert os.path.samefile(repo.working_dir, folder_path)
        assert _check_attr(repo, "note.md") == "7"

    def test_pin_is_idempotent_and_preserves_other_attributes(self):
        repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)
        with open(_attributes_path(repo), "w", encoding="utf-8") as f:
            f.write("*.bin binary")  # no trailing newline on purpose

        self.persistence._pin_conflict_marker_size(repo)
        self.persistence._pin_conflict_marker_size(repo)

        with open(_attributes_path(repo), encoding="utf-8") as f:
            lines = f.read().splitlines()
        assert lines == ["*.bin binary", PIN]

    def test_pin_write_failure_raises(self):
        repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)
        info_dir = os.path.join(_git_dir(repo), "info")
        shutil.rmtree(info_dir)
        # A regular file where the info directory should be makes the write fail.
        with open(info_dir, "w", encoding="utf-8") as f:
            f.write("not a directory\n")

        with pytest.raises(OSError):
            self.persistence._pin_conflict_marker_size(repo)

    def test_committed_gitattributes_cannot_raise_marker_size_during_merge(self):
        repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)
        _configure_identity(repo)
        workdir = repo.working_dir

        # Content a Relay collaborator could introduce: an in-tree .gitattributes.
        with open(os.path.join(workdir, ".gitattributes"), "w", encoding="utf-8") as f:
            f.write(IN_TREE_ATTRIBUTE)
        with open(os.path.join(workdir, "note.md"), "w", encoding="utf-8") as f:
            f.write("line1\nbase\nline3\n")
        _commit_all(repo, "base")

        repo.git.checkout("-b", "other")
        with open(os.path.join(workdir, "note.md"), "w", encoding="utf-8") as f:
            f.write("line1\ntheirs\nline3\n")
        _commit_all(repo, "theirs")

        repo.git.checkout("main")
        with open(os.path.join(workdir, "note.md"), "w", encoding="utf-8") as f:
            f.write("line1\nours\nline3\n")
        _commit_all(repo, "ours")

        # The in-tree attribute is visible to git but overridden by the pin.
        assert _check_attr(repo, "note.md") == "7"

        with pytest.raises(git.exc.GitCommandError):
            repo.git.merge("other")
        with open(os.path.join(workdir, "note.md"), encoding="utf-8") as f:
            conflicted = f.read()
        repo.git.merge("--abort")

        marker_lines = [line for line in conflicted.splitlines() if line.startswith("<<<<<<<")]
        assert marker_lines, conflicted
        assert all(line.startswith("<<<<<<< ") for line in marker_lines)
        assert all(not line.startswith("<<<<<<<<") for line in marker_lines)

    def test_committed_gitattributes_cannot_raise_marker_size_during_rebase(self):
        repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)
        _configure_identity(repo)
        workdir = repo.working_dir

        with open(os.path.join(workdir, ".gitattributes"), "w", encoding="utf-8") as f:
            f.write(IN_TREE_ATTRIBUTE)
        with open(os.path.join(workdir, "note.md"), "w", encoding="utf-8") as f:
            f.write("line1\nbase\nline3\n")
        _commit_all(repo, "base")

        repo.git.checkout("-b", "other")
        with open(os.path.join(workdir, "note.md"), "w", encoding="utf-8") as f:
            f.write("line1\ntheirs\nline3\n")
        _commit_all(repo, "theirs")

        repo.git.checkout("main")
        with open(os.path.join(workdir, "note.md"), "w", encoding="utf-8") as f:
            f.write("line1\nours\nline3\n")
        _commit_all(repo, "ours")

        repo.git.checkout("other")
        with pytest.raises(git.exc.GitCommandError):
            repo.git.rebase("main")
        with open(os.path.join(workdir, "note.md"), encoding="utf-8") as f:
            conflicted = f.read()
        repo.git.rebase("--abort")

        marker_lines = [line for line in conflicted.splitlines() if line.startswith("<<<<<<<")]
        assert marker_lines, conflicted
        assert all(not line.startswith("<<<<<<<<") for line in marker_lines)
