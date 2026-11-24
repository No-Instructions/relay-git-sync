"""Regression tests for the CVE-2026-111760 hardening.

git before 2.50.2 can be fooled into writing into ``.git`` while checking out a
crafted tree, and git executes repository hooks/config on the next command. Git Sync
clones and pulls operator-configured remotes, so the process must (a) never
re-initialise on top of a directory left behind by a failed clone, (b) commit
without running repository hooks, and (c) read hooks from an empty directory it
owns for every git command.
"""

import os
import shutil
import stat
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import git
import pytest

from persistence import PersistenceManager

# Longer than NAME_MAX (255) on Linux: the checkout step of a clone fails after the
# fetch succeeded, which is exactly the "clone succeeded, but checkout failed" path
# that leaves a repository behind.
UNCHECKOUTABLE_NAME = "x" * 300


def _configure_identity(repo: git.Repo) -> None:
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "test")
        cw.set_value("user", "email", "test@example.com")


def _plant_hook(hooks_dir: str, name: str, marker: str) -> None:
    os.makedirs(hooks_dir, exist_ok=True)
    hook = os.path.join(hooks_dir, name)
    with open(hook, "w", encoding="utf-8") as f:
        f.write(f"#!/bin/sh\necho hook-ran > '{marker}'\n")
    os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TestGitHardening:
    relay_id = "85a06712-af14-47bc-a859-e8106cc786e8"
    folder_id = "3667fcda-755e-472b-abea-4b4fc96873a9"
    GIT_ENV_KEYS = ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.saved_env = {k: os.environ.get(k) for k in os.environ if k.startswith("GIT_CONFIG")}
        for k in list(os.environ):
            if k.startswith("GIT_CONFIG"):
                del os.environ[k]
        with patch.object(PersistenceManager, "_initialize_all_git_repos"):
            self.persistence = PersistenceManager(self.temp_dir)

    def teardown_method(self):
        for k in list(os.environ):
            if k.startswith("GIT_CONFIG"):
                del os.environ[k]
        os.environ.update({k: v for k, v in self.saved_env.items() if v is not None})
        shutil.rmtree(self.temp_dir)

    # -- helpers -------------------------------------------------------------

    def _make_origin(self, uncheckoutable: bool) -> str:
        """Create a bare origin with one commit on main; optionally add a tree entry
        whose name cannot be checked out so the clone fails after fetching."""
        origin = os.path.join(self.temp_dir, "origin.git")
        git.Repo.init(origin, bare=True, initial_branch="main")
        src_dir = os.path.join(self.temp_dir, "src")
        src = git.Repo.init(src_dir, initial_branch="main")
        _configure_identity(src)
        with open(os.path.join(src_dir, "ok.txt"), "w", encoding="utf-8") as f:
            f.write("ok\n")
        src.git.add("ok.txt")
        src.index.commit("base", skip_hooks=True)
        if uncheckoutable:
            blob = src.git.hash_object("-w", "ok.txt")
            src.git.update_index("--add", "--cacheinfo", f"100644,{blob},{UNCHECKOUTABLE_NAME}")
            src.git.commit("-m", "longname")
        src.git.push(origin, "main:main")
        return origin

    def _connector(self, url: str):
        return SimpleNamespace(url=url, branch="main")

    # -- (a) failed clones leave nothing behind ------------------------------

    def test_failed_clone_leaves_no_leftover_repository(self):
        origin = self._make_origin(uncheckoutable=True)
        folder_path = self.persistence.get_folder_path(self.relay_id, self.folder_id)
        with patch.object(
            self.persistence.git_config,
            "get_connector_for_folder",
            return_value=self._connector(f"file://{origin}"),
        ):
            repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)

        # The fallback repository is a fresh local one, not the half-cloned remote.
        assert repo.working_tree_dir == folder_path
        assert not repo.remotes, "a failed clone must not leave its remote behind"
        assert sorted(os.listdir(folder_path)) == [".git", ".gitignore"]
        assert repo.head.commit.message == "Initial commit"
        # No staging directory survives beside the target either.
        siblings = [p for p in os.listdir(os.path.dirname(folder_path)) if ".clone-" in p]
        assert siblings == []

    def test_successful_clone_is_moved_into_place(self):
        origin = self._make_origin(uncheckoutable=False)
        folder_path = self.persistence.get_folder_path(self.relay_id, self.folder_id)
        with patch.object(
            self.persistence.git_config,
            "get_connector_for_folder",
            return_value=self._connector(f"file://{origin}"),
        ):
            repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)

        assert repo.working_tree_dir == folder_path
        assert [r.name for r in repo.remotes] == ["origin"]
        assert os.path.isfile(os.path.join(folder_path, "ok.txt"))
        siblings = [p for p in os.listdir(os.path.dirname(folder_path)) if ".clone-" in p]
        assert siblings == []

    # -- (b) commits never run repository hooks -------------------------------

    def test_initial_commit_skips_repository_hooks(self):
        # A hook can only pre-exist the very first commit if git's init template
        # installs it, so plant it through GIT_TEMPLATE_DIR.
        marker = os.path.join(self.temp_dir, "marker-initial")
        template = os.path.join(self.temp_dir, "template")
        _plant_hook(os.path.join(template, "hooks"), "pre-commit", marker)
        with patch.dict(os.environ, {"GIT_TEMPLATE_DIR": template}):
            with patch.object(
                self.persistence.git_config, "get_connector_for_folder", return_value=None
            ):
                repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)
        assert os.path.isfile(os.path.join(repo.git_dir, "hooks", "pre-commit"))
        assert repo.head.commit.message == "Initial commit"
        assert not os.path.exists(marker)

    def test_auto_sync_commit_skips_repository_hooks(self):
        with patch.object(
            self.persistence.git_config, "get_connector_for_folder", return_value=None
        ):
            repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)
        _configure_identity(repo)
        marker = os.path.join(self.temp_dir, "marker-sync")
        _plant_hook(os.path.join(repo.git_dir, "hooks"), "pre-commit", marker)
        with open(os.path.join(repo.working_tree_dir, "note.md"), "w", encoding="utf-8") as f:
            f.write("hello\n")
        with patch.object(self.persistence, "_is_repo_key_configured", return_value=True):
            assert self.persistence.commit_changes() is True
        assert repo.head.commit.message.startswith("Auto-sync:")
        assert not os.path.exists(marker)

    # -- (c) every git command reads hooks from our empty directory ------------

    def test_hooks_path_pinned_for_all_git_commands(self):
        hooks_dir = os.path.join(self.temp_dir, PersistenceManager.HOOKS_DISABLED_DIRNAME)
        assert os.path.isdir(hooks_dir) and os.listdir(hooks_dir) == []
        assert os.environ["GIT_CONFIG_COUNT"] == "1"
        assert os.environ["GIT_CONFIG_KEY_0"] == "core.hooksPath"
        assert os.environ["GIT_CONFIG_VALUE_0"] == hooks_dir

        with patch.object(
            self.persistence.git_config, "get_connector_for_folder", return_value=None
        ):
            repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)
        _configure_identity(repo)
        assert repo.git.config("core.hooksPath") == hooks_dir
        # A repository-planted hook is ignored even by plain git commands.
        marker = os.path.join(self.temp_dir, "marker-cli")
        _plant_hook(os.path.join(repo.git_dir, "hooks"), "pre-commit", marker)
        repo.git.commit("--allow-empty", "-m", "plain git commit")
        assert not os.path.exists(marker)

    def test_existing_hooks_path_override_is_replaced_not_duplicated(self):
        os.environ["GIT_CONFIG_COUNT"] = "2"
        os.environ["GIT_CONFIG_KEY_0"] = "user.name"
        os.environ["GIT_CONFIG_VALUE_0"] = "env-user"
        os.environ["GIT_CONFIG_KEY_1"] = "core.hooksPath"
        os.environ["GIT_CONFIG_VALUE_1"] = "/somewhere/else"
        hooks_dir = self.persistence._disable_repository_hooks()
        assert os.environ["GIT_CONFIG_COUNT"] == "2"
        assert os.environ["GIT_CONFIG_VALUE_1"] == hooks_dir
        assert os.environ["GIT_CONFIG_VALUE_0"] == "env-user"

    # -- (c) the hooks directory is ours even when data_dir is relative -------

    def test_relative_data_dir_cannot_be_hijacked_by_cloned_hooks(self, monkeypatch):
        """core.hooksPath must be absolute: git resolves a relative value against the
        worktree being operated on, so a remote shipping an executable
        ``.git-hooks-disabled/post-checkout`` would run it during the clone."""
        origin = self._make_origin(uncheckoutable=False)
        marker = os.path.join(self.temp_dir, "marker-clone")
        src_dir = os.path.join(self.temp_dir, "src")
        src = git.Repo(src_dir)
        _plant_hook(
            os.path.join(src_dir, PersistenceManager.HOOKS_DISABLED_DIRNAME),
            "post-checkout",
            marker,
        )
        src.git.add("-A")
        src.index.commit("ship hooks under the disabled-hooks name", skip_hooks=True)
        src.git.push(origin, "main:main")

        data_dir = os.path.join(self.temp_dir, "data")
        os.makedirs(data_dir)
        monkeypatch.chdir(data_dir)
        with patch.object(PersistenceManager, "_initialize_all_git_repos"):
            persistence = PersistenceManager(".")

        hooks_path = os.environ["GIT_CONFIG_VALUE_0"]
        assert os.path.isabs(hooks_path)
        assert hooks_path == os.path.join(data_dir, PersistenceManager.HOOKS_DISABLED_DIRNAME)

        with patch.object(
            persistence.git_config,
            "get_connector_for_folder",
            return_value=self._connector(f"file://{origin}"),
        ):
            repo = persistence.init_git_repo(self.relay_id, self.folder_id)

        tracked_hook = os.path.join(
            repo.working_tree_dir, PersistenceManager.HOOKS_DISABLED_DIRNAME, "post-checkout"
        )
        assert os.path.isfile(tracked_hook), "the hostile hook was checked out as content"
        assert not os.path.exists(marker), "a hook shipped by the remote ran during clone"
        # Plain git commands in the clone keep resolving hooks outside the worktree.
        assert repo.git.config("core.hooksPath") == hooks_path
        _configure_identity(repo)
        repo.git.commit("--allow-empty", "-m", "after clone")
        assert not os.path.exists(marker)
