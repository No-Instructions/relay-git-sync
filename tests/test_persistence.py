#!/usr/bin/env python3

import json
import logging
import os
import shutil
import tempfile
import time
from unittest.mock import MagicMock, mock_open, patch

import git
import pytest

from persistence import PersistenceManager, SSHKeyManager
from s3rn import S3RemoteCanvas, S3RemoteDocument, S3RemoteFile, S3RemoteFolder


class TestPathSanitization:
    """Test path sanitization security measures"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.persistence = PersistenceManager(self.temp_dir)
        self.base_dir = os.path.join(self.temp_dir, "test_base")
        os.makedirs(self.base_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_sanitize_path_normal_file(self):
        """Test normal file paths are handled correctly"""
        result = self.persistence._sanitize_path("normal/file.txt", self.base_dir)
        expected = os.path.join(self.base_dir, "normal", "file.txt")
        assert result == expected

    def test_sanitize_path_removes_leading_slashes(self):
        """Test leading slashes are removed"""
        result = self.persistence._sanitize_path("/folder/file.txt", self.base_dir)
        expected = os.path.join(self.base_dir, "folder", "file.txt")
        assert result == expected

    def test_sanitize_path_blocks_directory_traversal(self):
        """Test directory traversal attempts are blocked"""
        with pytest.raises(ValueError, match="contains directory traversal"):
            self.persistence._sanitize_path("../evil.txt", self.base_dir)

        with pytest.raises(ValueError, match="contains directory traversal"):
            self.persistence._sanitize_path("folder/../../../evil.txt", self.base_dir)

    def test_sanitize_path_allows_dots_in_filenames(self):
        """Test that filenames containing '..' are allowed (not traversal)"""
        # Ellipsis in filename should not trigger traversal check
        result = self.persistence._sanitize_path("folder/file with dots....md", self.base_dir)
        assert result.endswith("folder/file with dots....md")

    def test_sanitize_path_blocks_escape_attempts(self):
        """Test various escape attempts are blocked"""
        # Test with ".." components which the sanitizer actually checks for
        with pytest.raises(ValueError, match="contains directory traversal"):
            self.persistence._sanitize_path("../../etc/passwd", self.base_dir)

        # Test another traversal attempt
        with pytest.raises(ValueError, match="contains directory traversal"):
            self.persistence._sanitize_path("folder/../../../evil.txt", self.base_dir)

    def test_sanitize_path_empty_path_raises_error(self):
        """Test empty path raises ValueError"""
        with pytest.raises(ValueError, match="Empty path provided"):
            self.persistence._sanitize_path("", self.base_dir)

        with pytest.raises(ValueError, match="Empty path provided"):
            self.persistence._sanitize_path(None, self.base_dir)

    def test_sanitize_path_base_directory_escape_blocked(self):
        """Test attempts to reference base directory itself are blocked"""
        with pytest.raises(ValueError, match="attempts to escape"):
            self.persistence._sanitize_path(".", self.base_dir)


class TestResourceIndexManagement:
    """Test resource index operations"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        with patch.object(PersistenceManager, "_initialize_all_git_repos"):
            self.persistence = PersistenceManager(self.temp_dir)
        self.relay_id = "test-relay-123"

        # Initialize test data
        self.persistence.document_hashes[self.relay_id] = {
            "doc-123": "hash123",
            "canvas-456": "hash456",
        }
        self.persistence.filemeta_folders[self.relay_id] = {
            "folder-789": {
                "/test.md": {"id": "doc-123", "type": "document"},
                "/canvas.canvas": {"id": "canvas-456", "type": "canvas"},
            }
        }
        self.persistence.local_file_state[self.relay_id] = {
            "folder-789": {
                "/test.md": {"doc_id": "doc-123", "type": "document", "hash": "hash123"},
                "/canvas.canvas": {"doc_id": "canvas-456", "type": "canvas", "hash": "hash456"},
            }
        }

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_build_resource_index(self):
        """Test resource index is built correctly"""
        self.persistence._build_resource_index(self.relay_id)

        index = self.persistence.resource_index[self.relay_id]

        # Check folder is indexed
        assert "folder-789" in index
        assert index["folder-789"]["type"] == "folder"

        # Check documents are indexed
        assert "doc-123" in index
        assert index["doc-123"]["type"] == "document"
        assert index["doc-123"]["folder_id"] == "folder-789"
        assert index["doc-123"]["path"] == "/test.md"

        assert "canvas-456" in index
        assert index["canvas-456"]["type"] == "canvas"
        assert index["canvas-456"]["folder_id"] == "folder-789"
        assert index["canvas-456"]["path"] == "/canvas.canvas"

    def test_lookup_resource_document(self):
        """Test resource lookup returns correct S3RN objects"""
        self.persistence._build_resource_index(self.relay_id)

        # Lookup document
        resource = self.persistence.lookup_resource(self.relay_id, "doc-123")
        assert isinstance(resource, S3RemoteDocument)
        assert resource.relay_id == self.relay_id
        assert resource.folder_id == "folder-789"
        assert resource.document_id == "doc-123"

    def test_lookup_resource_canvas(self):
        """Test canvas resource lookup"""
        self.persistence._build_resource_index(self.relay_id)

        resource = self.persistence.lookup_resource(self.relay_id, "canvas-456")
        assert isinstance(resource, S3RemoteCanvas)
        assert resource.relay_id == self.relay_id
        assert resource.folder_id == "folder-789"
        assert resource.canvas_id == "canvas-456"

    def test_lookup_resource_folder(self):
        """Test folder resource lookup"""
        self.persistence._build_resource_index(self.relay_id)

        resource = self.persistence.lookup_resource(self.relay_id, "folder-789")
        assert isinstance(resource, S3RemoteFolder)
        assert resource.relay_id == self.relay_id
        assert resource.folder_id == "folder-789"

    def test_get_resource_path(self):
        """Test getting resource path from index"""
        self.persistence._build_resource_index(self.relay_id)

        path = self.persistence.get_resource_path(self.relay_id, "doc-123")
        assert path == "/test.md"

        path = self.persistence.get_resource_path(self.relay_id, "canvas-456")
        assert path == "/canvas.canvas"

        # Folders don't have paths
        path = self.persistence.get_resource_path(self.relay_id, "folder-789")
        assert path is None

    def test_update_resource_index_for_document(self):
        """Test updating resource index when document is added"""
        self.persistence._build_resource_index(self.relay_id)

        # Add new document to index
        metadata = {"id": "new-doc-999", "type": "document"}
        self.persistence.update_resource_index_for_document(
            self.relay_id, "new-doc-999", "folder-789", "/new.md", metadata
        )

        # Verify it was added
        resource = self.persistence.lookup_resource(self.relay_id, "new-doc-999")
        assert isinstance(resource, S3RemoteDocument)
        assert resource.document_id == "new-doc-999"

        path = self.persistence.get_resource_path(self.relay_id, "new-doc-999")
        assert path == "/new.md"

    def test_remove_resource_from_index(self):
        """Test removing resource from index"""
        self.persistence._build_resource_index(self.relay_id)

        # Verify resource exists
        assert self.persistence.lookup_resource(self.relay_id, "doc-123") is not None

        # Remove it
        self.persistence.remove_resource_from_index(self.relay_id, "doc-123")

        # Verify it's gone
        assert self.persistence.lookup_resource(self.relay_id, "doc-123") is None

    def test_skip_compound_ids(self):
        """Test that compound IDs are skipped during index building"""
        # Add compound ID to test data
        self.persistence.document_hashes[self.relay_id][
            "compound-id-with-many-dashes-12345"
        ] = "hash999"
        self.persistence.local_file_state[self.relay_id]["folder-789"]["/compound.md"] = {
            "doc_id": "compound-id-with-many-dashes-12345",
            "type": "document",
        }

        self.persistence._build_resource_index(self.relay_id)

        # Compound ID should not be in index
        assert (
            "compound-id-with-many-dashes-12345"
            not in self.persistence.resource_index[self.relay_id]
        )

        # Regular IDs should still be there
        assert "doc-123" in self.persistence.resource_index[self.relay_id]


class TestConfiguredFolderGuardrails:
    relay_id = "85a06712-af14-47bc-a859-e8106cc786e8"
    other_relay_id = "3017ef4d-9b10-48f9-a52e-12f48331e21d"
    configured_folder_id = "3667fcda-755e-472b-abea-4b4fc96873a9"
    stale_folder_id = "bcb3e341-6ddf-4a3d-9e8f-8fc3773cd49f"

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        config_path = os.path.join(self.temp_dir, "git_connectors.toml")
        with open(config_path, "w") as f:
            f.write(
                f"""
[relay]
id = "{self.relay_id}"
url = "https://auth.system3.dev"

[[git_connector]]
shared_folder_id = "{self.configured_folder_id}"
url = "git@github.com:No-Instructions/git-sync-test.git"
branch = "main"
remote_name = "origin"
prefix = "notes"
"""
            )
        with patch.object(PersistenceManager, "_initialize_all_git_repos"):
            self.persistence = PersistenceManager(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_resource_index_ignores_unconfigured_persisted_folders(self):
        self.persistence.filemeta_folders[self.relay_id] = {
            self.configured_folder_id: {"/home.md": {"id": "configured-doc", "type": "document"}},
            self.stale_folder_id: {"/old.md": {"id": "stale-doc", "type": "document"}},
        }
        self.persistence.local_file_state[self.relay_id] = {
            self.configured_folder_id: {
                "/home.md": {
                    "doc_id": "configured-doc",
                    "type": "document",
                    "hash": "configured-hash",
                }
            },
            self.stale_folder_id: {
                "/old.md": {
                    "doc_id": "stale-doc",
                    "type": "document",
                    "hash": "stale-hash",
                }
            },
        }

        self.persistence._build_resource_index(self.relay_id)

        index = self.persistence.resource_index[self.relay_id]
        assert self.configured_folder_id in index
        assert "configured-doc" in index
        assert self.stale_folder_id not in index
        assert "stale-doc" not in index

    def test_configured_connectors_reject_folders_from_other_relays(self):
        self.persistence.filemeta_folders[self.other_relay_id] = {
            self.stale_folder_id: {"/private.md": {"id": "other-relay-doc", "type": "document"}}
        }
        self.persistence.local_file_state[self.other_relay_id] = {
            self.stale_folder_id: {
                "/private.md": {
                    "doc_id": "other-relay-doc",
                    "type": "document",
                    "hash": "other-relay-hash",
                }
            }
        }
        self.persistence.document_hashes[self.other_relay_id] = {
            "other-relay-doc": "other-relay-hash"
        }

        assert not self.persistence.should_sync_folder(self.other_relay_id, self.stale_folder_id)

        self.persistence._build_resource_index(self.other_relay_id)

        assert self.persistence.resource_index[self.other_relay_id] == {}

    def test_commit_changes_skips_unconfigured_repos_when_config_exists(self):
        configured_repo = MagicMock()
        configured_repo.is_dirty.return_value = True
        configured_repo.untracked_files = []
        configured_repo.remotes = []

        stale_repo = MagicMock()
        stale_repo.is_dirty.return_value = True
        stale_repo.untracked_files = []
        stale_repo.remotes = []

        self.persistence.git_repos = {
            f"{self.relay_id}/{self.configured_folder_id}": configured_repo,
            f"{self.relay_id}/{self.stale_folder_id}": stale_repo,
        }

        assert self.persistence.commit_changes() is True

        configured_repo.git.add.assert_called_once_with(A=True)
        configured_repo.index.commit.assert_called_once()
        stale_repo.git.add.assert_not_called()
        stale_repo.index.commit.assert_not_called()

    def test_push_all_repos_skips_unconfigured_repos_when_config_exists(self):
        configured_repo = MagicMock()
        configured_repo.remotes = [object()]
        stale_repo = MagicMock()
        stale_repo.remotes = [object()]
        self.persistence.git_repos = {
            f"{self.relay_id}/{self.configured_folder_id}": configured_repo,
            f"{self.relay_id}/{self.stale_folder_id}": stale_repo,
        }
        self.persistence._push_to_remote = MagicMock()

        assert self.persistence.push_all_repos() == 1

        self.persistence._push_to_remote.assert_called_once_with(
            f"{self.relay_id}/{self.configured_folder_id}", configured_repo
        )


class TestFileOperations:
    """Test file write, move, delete operations"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.persistence = PersistenceManager(self.temp_dir)
        self.relay_id = "test-relay-123"
        self.folder_id = "folder-789"

        # Create folder structure
        self.folder_path = os.path.join(self.temp_dir, "repos", self.relay_id, self.folder_id)
        os.makedirs(self.folder_path, exist_ok=True)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_write_file_content(self):
        """Test writing file content"""
        document_resource = S3RemoteDocument(self.relay_id, self.folder_id, "doc-123")
        content = "# Test Document\nThis is test content."
        file_hash = "testhash123"

        result_path = self.persistence.write_file_content(
            document_resource, "/test.md", content, file_hash
        )

        # Check file was written
        assert os.path.exists(result_path)
        with open(result_path, "r") as f:
            assert f.read() == content

        # Check local state was updated
        state = self.persistence.local_file_state[self.relay_id][self.folder_id]["/test.md"]
        assert state["doc_id"] == "doc-123"
        assert state["hash"] == file_hash
        assert state["type"] == "document"

    def test_write_binary_file_content(self):
        """Test writing binary file content"""
        file_resource = S3RemoteFile(self.relay_id, self.folder_id, "file-123")
        binary_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        file_hash = "binaryhash123"

        result_path = self.persistence.write_binary_file_content(
            file_resource, "/image.png", binary_content, file_hash
        )

        # Check file was written
        assert os.path.exists(result_path)
        with open(result_path, "rb") as f:
            assert f.read() == binary_content

        # Check local state was updated
        state = self.persistence.local_file_state[self.relay_id][self.folder_id]["/image.png"]
        assert state["doc_id"] == "file-123"
        assert state["hash"] == file_hash
        assert state["type"] == "file"

    def test_write_file_creates_parent_directories(self):
        """Test that parent directories are created"""
        document_resource = S3RemoteDocument(self.relay_id, self.folder_id, "doc-123")
        content = "Test content"

        result_path = self.persistence.write_file_content(
            document_resource, "/deep/nested/path/test.md", content
        )

        # Check file was written and parent dirs created
        assert os.path.exists(result_path)
        assert os.path.exists(os.path.dirname(result_path))

    def test_write_file_blocks_directory_overwrite(self):
        """Test writing file over existing directory fails"""
        # Create directory first
        dir_path = os.path.join(self.folder_path, "testdir")
        os.makedirs(dir_path)

        document_resource = S3RemoteDocument(self.relay_id, self.folder_id, "doc-123")

        with pytest.raises(ValueError, match="exists as directory"):
            self.persistence.write_file_content(document_resource, "/testdir", "content")

    def test_move_file(self):
        """Test moving files"""
        document_resource = S3RemoteDocument(self.relay_id, self.folder_id, "doc-123")

        # Create initial file
        initial_content = "Initial content"
        self.persistence.write_file_content(document_resource, "/old.md", initial_content)

        # Move file
        old_path, new_path = self.persistence.move_file(document_resource, "/old.md", "/new.md")

        # Check old file is gone and new file exists
        assert not os.path.exists(old_path)
        assert os.path.exists(new_path)

        # Check content is preserved
        with open(new_path, "r") as f:
            assert f.read() == initial_content

        # Check local state was updated
        folder_state = self.persistence.local_file_state[self.relay_id][self.folder_id]
        assert "/old.md" not in folder_state
        assert "/new.md" in folder_state
        assert folder_state["/new.md"]["doc_id"] == "doc-123"

    def test_delete_file(self):
        """Test deleting files"""
        folder_resource = S3RemoteFolder(self.relay_id, self.folder_id)

        # Create file first
        document_resource = S3RemoteDocument(self.relay_id, self.folder_id, "doc-123")
        self.persistence.write_file_content(document_resource, "/delete_me.md", "content")

        # Delete file
        deleted_path = self.persistence.delete_file(folder_resource, "/delete_me.md")

        # Check file is gone
        assert not os.path.exists(deleted_path)

        # Check local state was updated
        folder_state = self.persistence.local_file_state.get(self.relay_id, {}).get(
            self.folder_id, {}
        )
        assert "/delete_me.md" not in folder_state

    def test_create_directory(self):
        """Test creating directories"""
        folder_resource = S3RemoteFolder(self.relay_id, self.folder_id)

        result_path = self.persistence.create_directory(folder_resource, "/new_directory")

        # Check directory was created
        assert os.path.exists(result_path)
        assert os.path.isdir(result_path)


class TestLocalFileStateFunctions:
    """Test local file state tracking functions"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.persistence = PersistenceManager(self.temp_dir)
        self.relay_id = "test-relay-123"
        self.folder_id = "folder-789"

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_find_local_file_by_doc_id(self):
        """Test finding local files by document ID"""
        # Setup test state
        self.persistence.local_file_state[self.relay_id] = {
            self.folder_id: {
                "/test1.md": {"doc_id": "doc-123", "type": "document"},
                "/test2.md": {"doc_id": "doc-456", "type": "document"},
                "/canvas.canvas": {"doc_id": "canvas-789", "type": "canvas"},
            }
        }

        # Test finding existing files
        path = self.persistence.find_local_file_by_doc_id(self.relay_id, self.folder_id, "doc-123")
        assert path == "/test1.md"

        path = self.persistence.find_local_file_by_doc_id(
            self.relay_id, self.folder_id, "canvas-789"
        )
        assert path == "/canvas.canvas"

        # Test non-existent file
        path = self.persistence.find_local_file_by_doc_id(
            self.relay_id, self.folder_id, "nonexistent"
        )
        assert path is None

    def test_update_local_file_state(self):
        """Test updating local file state"""
        document_resource = S3RemoteDocument(self.relay_id, self.folder_id, "doc-123")

        self.persistence.update_local_file_state(document_resource, "/test.md", "hash123")

        # Check state was updated
        state = self.persistence.local_file_state[self.relay_id][self.folder_id]["/test.md"]
        assert state["doc_id"] == "doc-123"
        assert state["hash"] == "hash123"
        assert state["type"] == "document"
        assert "modified" in state

    def test_remove_local_file_state(self):
        """Test removing local file state"""
        # Setup initial state
        self.persistence.local_file_state[self.relay_id] = {
            self.folder_id: {"/test.md": {"doc_id": "doc-123", "type": "document"}}
        }

        # Remove state
        self.persistence.remove_local_file_state(self.relay_id, self.folder_id, "/test.md")

        # Check it's gone
        folder_state = self.persistence.local_file_state[self.relay_id][self.folder_id]
        assert "/test.md" not in folder_state


class TestPersistentDataManagement:
    """Test loading and saving persistent data"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.persistence = PersistenceManager(self.temp_dir)
        self.relay_id = "test-relay-123"

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_save_and_load_document_hashes(self):
        """Test saving and loading document hashes"""
        test_hashes = {"doc-123": "hash123", "doc-456": "hash456"}
        self.persistence.document_hashes[self.relay_id] = test_hashes

        # Save data
        self.persistence.save_persistent_data(self.relay_id)

        # Clear in-memory data
        self.persistence.document_hashes[self.relay_id] = {}

        # Load data
        self.persistence.load_persistent_data(self.relay_id)

        # Check data was restored
        assert self.persistence.document_hashes[self.relay_id] == test_hashes

    def test_save_and_load_subdoc_heads(self):
        test_heads = {"doc-123": "head123", "doc-456": "head456"}
        self.persistence.subdoc_heads[self.relay_id] = test_heads

        self.persistence.save_persistent_data(self.relay_id)
        self.persistence.subdoc_heads[self.relay_id] = {}
        self.persistence.load_persistent_data(self.relay_id)

        assert self.persistence.subdoc_heads[self.relay_id] == test_heads

    def test_save_and_load_filemeta(self):
        """Test saving and loading filemeta"""
        test_filemeta = {"folder-789": {"/test.md": {"id": "doc-123", "type": "document"}}}
        self.persistence.filemeta_folders[self.relay_id] = test_filemeta

        # Save and reload
        self.persistence.save_persistent_data(self.relay_id)
        self.persistence.filemeta_folders[self.relay_id] = {}
        self.persistence.load_persistent_data(self.relay_id)

        # Check data was restored
        assert self.persistence.filemeta_folders[self.relay_id] == test_filemeta

    def test_load_nonexistent_data_creates_empty_structures(self):
        """Test loading nonexistent data creates empty structures"""
        self.persistence.load_persistent_data(self.relay_id)

        # Check empty structures were created
        assert self.relay_id in self.persistence.document_hashes
        assert self.relay_id in self.persistence.subdoc_heads
        assert self.relay_id in self.persistence.filemeta_folders
        assert self.relay_id in self.persistence.local_file_state
        assert self.relay_id in self.persistence.resource_index

        assert self.persistence.document_hashes[self.relay_id] == {}
        assert self.persistence.subdoc_heads[self.relay_id] == {}
        assert self.persistence.filemeta_folders[self.relay_id] == {}
        assert self.persistence.local_file_state[self.relay_id] == {}


@patch("git.Repo")
class TestGitOperations:
    """Test Git repository operations"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.persistence = PersistenceManager(self.temp_dir)
        self.relay_id = "test-relay-123"
        self.folder_id = "folder-789"

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def test_init_git_repo_creates_new_repo(self, mock_git_repo):
        """Test initializing new git repository"""
        # Mock git.Repo to raise InvalidGitRepositoryError first (no existing repo)
        mock_git_repo.side_effect = [git.InvalidGitRepositoryError, MagicMock()]
        mock_git_repo.init.return_value = MagicMock()

        repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)

        # Check repo was initialized
        expected_path = self.persistence.get_folder_path(self.relay_id, self.folder_id)
        mock_git_repo.init.assert_called_once_with(expected_path, initial_branch="main")

    def test_init_git_repo_uses_existing_repo(self, mock_git_repo):
        """Test using existing git repository"""
        mock_repo = MagicMock()
        mock_git_repo.return_value = mock_repo

        repo = self.persistence.init_git_repo(self.relay_id, self.folder_id)

        # Check existing repo was used
        expected_path = self.persistence.get_folder_path(self.relay_id, self.folder_id)
        mock_git_repo.assert_called_with(expected_path)
        assert repo == mock_repo

    @patch("os.path.exists")
    def test_commit_changes_with_dirty_repo(self, mock_exists, mock_git_repo):
        """Test committing changes when repository is dirty"""
        mock_exists.return_value = True

        # Setup mock repo
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = True
        mock_repo.untracked_files = ["new_file.txt"]

        self.persistence.git_repos[f"{self.relay_id}/{self.folder_id}"] = mock_repo

        result = self.persistence.commit_changes()

        # Check git operations were called
        mock_repo.git.add.assert_called_once_with(A=True)
        mock_repo.index.commit.assert_called_once()
        assert result is True

    @patch("os.path.exists")
    def test_commit_changes_with_clean_repo(self, mock_exists, mock_git_repo):
        """Test no commit when repository is clean"""
        mock_exists.return_value = True

        # Setup clean mock repo
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.untracked_files = []

        self.persistence.git_repos[f"{self.relay_id}/{self.folder_id}"] = mock_repo

        result = self.persistence.commit_changes()

        # Check no git operations were called
        mock_repo.git.add.assert_not_called()
        mock_repo.index.commit.assert_not_called()
        assert result is False


class TestSSHKeyManager:
    """Test SSH key management functionality"""

    def setup_method(self):
        # Create temp directory for SSH files
        self.temp_dir = tempfile.mkdtemp()

        # Generate a test key for testing
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        # Generate test private key
        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )

        # Serialize private key
        self.test_private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        # Store original env var
        self.original_ssh_key = os.environ.get("SSH_PRIVATE_KEY")

        # Set test key
        os.environ["SSH_PRIVATE_KEY"] = self.test_private_pem

    def teardown_method(self):
        # Clean up temp directory
        shutil.rmtree(self.temp_dir)

        # Restore original env var
        if self.original_ssh_key:
            os.environ["SSH_PRIVATE_KEY"] = self.original_ssh_key
        else:
            os.environ.pop("SSH_PRIVATE_KEY", None)

    def test_missing_ssh_key_raises_error(self):
        """Test that missing SSH_PRIVATE_KEY raises ValueError"""
        os.environ.pop("SSH_PRIVATE_KEY", None)

        with pytest.raises(ValueError, match="SSH_PRIVATE_KEY not found"):
            SSHKeyManager(self.temp_dir)

    def test_creates_temporary_key_file(self):
        """Test SSH key manager creates key files"""
        ssh_manager = SSHKeyManager(self.temp_dir)

        # Check key files exist and have correct permissions
        assert os.path.exists(ssh_manager.private_key_path)
        assert os.path.exists(ssh_manager.public_key_path)

        stat_info = os.stat(ssh_manager.private_key_path)
        assert oct(stat_info.st_mode)[-3:] == "600"

        # Check file contains the key
        with open(ssh_manager.private_key_path, "r") as f:
            content = f.read()
        assert content == self.test_private_pem

    def test_restores_trailing_newline_stripped_by_secret_store(self):
        os.environ["SSH_PRIVATE_KEY"] = self.test_private_pem.rstrip("\r\n")

        ssh_manager = SSHKeyManager(self.temp_dir)

        with open(ssh_manager.private_key_path, "rb") as f:
            content = f.read()
        assert content.endswith(b"\n")
        assert not content.endswith(b"\n\n")
        assert ssh_manager.get_public_key().startswith("ssh-rsa ")

    def test_get_public_key_extracts_from_private(self):
        """Test extracting public key from private key"""
        ssh_manager = SSHKeyManager(self.temp_dir)

        public_key = ssh_manager.get_public_key()

        # Check it's a valid SSH public key format
        assert public_key.startswith("ssh-rsa ")
        assert len(public_key.split()) >= 2  # ssh-rsa + key data

    def test_known_hosts_are_written_when_configured(self):
        """Test writing configured SSH known host keys"""
        known_hosts = ["git.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestHostKey"]

        ssh_manager = SSHKeyManager(self.temp_dir, known_hosts=known_hosts)

        assert os.path.exists(ssh_manager.known_hosts_path)
        with open(ssh_manager.known_hosts_path, "r") as f:
            assert f.read() == known_hosts[0] + "\n"

    def test_persistence_uses_configured_known_hosts_file(self):
        """Test configured known_hosts is wired into GIT_SSH_COMMAND"""
        known_host = "git.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestHostKey"
        with open(os.path.join(self.temp_dir, "git_connectors.toml"), "w") as f:
            f.write(
                f"""
known_hosts = ["{known_host}"]

[relay]
id = "85a06712-af14-47bc-a859-e8106cc786e8"
url = "https://auth.system3.dev"
"""
            )

        with patch.object(PersistenceManager, "_initialize_all_git_repos"):
            persistence = PersistenceManager(self.temp_dir)

        assert persistence.ssh_key_manager.known_hosts == [known_host]
        assert os.path.exists(persistence.ssh_key_manager.known_hosts_path)
        ssh_command = os.environ["GIT_SSH_COMMAND"]
        assert f"UserKnownHostsFile={persistence.ssh_key_manager.known_hosts_path}" in ssh_command
        assert "StrictHostKeyChecking=yes" in ssh_command
        assert "IdentitiesOnly=yes" in ssh_command

    def test_persistence_uses_inferred_github_known_hosts_file(self):
        """Test GitHub SSH remotes get provider known host keys"""
        with open(os.path.join(self.temp_dir, "git_connectors.toml"), "w") as f:
            f.write(
                """
[relay]
id = "85a06712-af14-47bc-a859-e8106cc786e8"
url = "https://auth.system3.dev"

[[git_connector]]
shared_folder_id = "3667fcda-755e-472b-abea-4b4fc96873a9"
url = "git@github.com:example/repository.git"
"""
            )

        github_known_hosts = ["github.com ssh-ed25519 AAAATestHostKey"]
        with (
            patch("git_config.fetch_known_hosts_for_host", return_value=github_known_hosts),
            patch.object(PersistenceManager, "_initialize_all_git_repos"),
        ):
            persistence = PersistenceManager(self.temp_dir)

        assert persistence.ssh_key_manager.known_hosts == github_known_hosts
        assert os.path.exists(persistence.ssh_key_manager.known_hosts_path)
        ssh_command = os.environ["GIT_SSH_COMMAND"]
        assert f"UserKnownHostsFile={persistence.ssh_key_manager.known_hosts_path}" in ssh_command
        assert "StrictHostKeyChecking=yes" in ssh_command

    def test_invalid_key_format_raises_error(self):
        """Test that invalid key format raises RuntimeError"""
        os.environ["SSH_PRIVATE_KEY"] = "not a valid key"

        with pytest.raises(RuntimeError, match="Failed to setup SSH keys"):
            SSHKeyManager(self.temp_dir)  # This should fail during initialization


if __name__ == "__main__":
    pytest.main([__file__])


class TestOutOfBandPushRecovery:
    """Connector must keep syncing when something else pushed to its branch first
    (a second connector sharing the repo under a different prefix, or a human).

    Previously this wedged permanently: the pre-commit pull compared against a
    never-refreshed remote-tracking ref (logging "up to date with remote" while
    actually behind), and the rejected push was invisible to the recovery path
    because Remote.push() reports rejection via PushInfo flags rather than raising.
    """

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

        self.remote_path = os.path.join(self.temp_dir, "remote.git")
        git.Repo.init(self.remote_path, bare=True, initial_branch="main")

        seed_path = os.path.join(self.temp_dir, "seed")
        seed = git.Repo.clone_from(self.remote_path, seed_path)
        self._configure_identity(seed)
        with open(os.path.join(seed_path, "seed.md"), "w") as f:
            f.write("seed\n")
        seed.git.add(A=True)
        seed.index.commit("initial")
        seed.git.push("origin", "main")

        # "other" simulates whoever else pushes to the branch; "mine" is this
        # connector's clone, driven through the real PersistenceManager paths.
        self.other_path = os.path.join(self.temp_dir, "other")
        self.mine_path = os.path.join(self.temp_dir, "mine")
        self.other = git.Repo.clone_from(self.remote_path, self.other_path)
        self.mine = git.Repo.clone_from(self.remote_path, self.mine_path)
        self._configure_identity(self.other)
        self._configure_identity(self.mine)

        self.pm = PersistenceManager(self.temp_dir)
        self.repo_key = "relay/folder"
        self.pm.git_repos[self.repo_key] = self.mine

    def teardown_method(self):
        shutil.rmtree(self.temp_dir)

    def _configure_identity(self, repo):
        repo.git.config("user.email", "test@example.com")
        repo.git.config("user.name", "test")

    def _unset_upstream(self, repo):
        repo.git.config("--unset", "branch.main.remote")
        repo.git.config("--unset", "branch.main.merge")
        assert repo.active_branch.tracking_branch() is None

    def _other_pushes(self, name):
        with open(os.path.join(self.other_path, name), "w") as f:
            f.write("content\n")
        self.other.git.add(A=True)
        self.other.index.commit(f"other: {name}")
        self.other.git.pull("--rebase", "origin", "main")
        self.other.git.push("origin", "main")

    def _reached_remote(self, hexsha):
        return hexsha in git.Repo(self.remote_path).git.rev_list("main").splitlines()

    def test_commit_cycle_syncs_despite_remote_advancing(self):
        for n in range(3):
            self._other_pushes(f"other-{n}.md")
            with open(os.path.join(self.mine_path, f"mine-{n}.md"), "w") as f:
                f.write("mine\n")
            assert self.pm.commit_changes()
            assert self._reached_remote(
                self.mine.head.commit.hexsha
            ), f"cycle {n}: connector commit never reached the remote"

    def test_rejected_push_triggers_recovery(self):
        # A commit already created on a stale parent: the race window where the
        # remote advanced between this connector's pull and its push.
        self._other_pushes("racing.md")
        with open(os.path.join(self.mine_path, "mine.md"), "w") as f:
            f.write("mine\n")
        self.mine.git.add(A=True)
        self.mine.index.commit("stale-parent commit")

        self.pm._push_to_remote(self.repo_key, self.mine)

        assert self._reached_remote(
            self.mine.head.commit.hexsha
        ), "rejected push was not recovered (rebase + re-push)"

    def _remote_rejects_pushes(self):
        hook = os.path.join(self.remote_path, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)

    def _commit_mine(self, name, **env):
        with open(os.path.join(self.mine_path, name), "w") as f:
            f.write("mine\n")
        self.mine.git.add(A=True)
        with self.mine.git.custom_environment(**env):
            self.mine.git.commit("-m", f"mine: {name}")

    def test_remote_rejection_logs_reason(self, caplog):
        self._remote_rejects_pushes()
        self._commit_mine("mine.md")

        with caplog.at_level(logging.INFO, logger="persistence"):
            self.pm._push_to_remote(self.repo_key, self.mine)

        still_failing = [
            r.getMessage() for r in caplog.records if "Push still failing" in r.getMessage()
        ]
        assert still_failing and "pre-receive hook declined" in still_failing[0]
        assert not any("Push stalled" in r.getMessage() for r in caplog.records)

    def test_long_rejected_push_logs_stall(self, caplog):
        self._remote_rejects_pushes()
        old = "2026-01-01T00:00:00+00:00"
        self._commit_mine("old.md", GIT_COMMITTER_DATE=old, GIT_AUTHOR_DATE=old)
        self._commit_mine("new.md")

        with caplog.at_level(logging.INFO, logger="persistence"):
            self.pm._push_to_remote(self.repo_key, self.mine)

        assert any(
            "Push stalled over 30 minutes for relay/folder: 2 commits unpushed" in r.getMessage()
            for r in caplog.records
        )

    def test_persistent_transport_failure_logs_stall(self, caplog):
        self.mine.remotes.origin.set_url(os.path.join(self.temp_dir, "missing.git"))
        old = "2026-01-01T00:00:00+00:00"
        self._commit_mine("old.md", GIT_COMMITTER_DATE=old, GIT_AUTHOR_DATE=old)

        with caplog.at_level(logging.INFO, logger="persistence"):
            self.pm._push_to_remote(self.repo_key, self.mine)

        assert self.pm.has_failed_pushes
        assert any(
            "Push stalled over 30 minutes for relay/folder: 1 commits unpushed" in r.getMessage()
            for r in caplog.records
        )

    def test_failed_push_is_retried_without_a_new_edit(self):
        self._remote_rejects_pushes()
        self._commit_mine("mine.md")
        self.pm._push_to_remote(self.repo_key, self.mine)
        assert self.pm.has_failed_pushes

        os.remove(os.path.join(self.remote_path, "hooks", "pre-receive"))
        self.pm.commit_changes()
        assert not self._reached_remote(
            self.mine.head.commit.hexsha
        ), "retried before PUSH_RETRY_INTERVAL_SECONDS elapsed"

        self.pm.PUSH_RETRY_INTERVAL_SECONDS = 0
        self.pm.commit_changes()
        assert self._reached_remote(self.mine.head.commit.hexsha)
        assert not self.pm.has_failed_pushes

    def test_new_edit_waits_for_push_retry_interval(self, caplog):
        self._remote_rejects_pushes()
        self._commit_mine("mine.md")
        self.pm._push_to_remote(self.repo_key, self.mine)
        os.remove(os.path.join(self.remote_path, "hooks", "pre-receive"))

        with open(os.path.join(self.mine_path, "edit.md"), "w") as f:
            f.write("edit\n")
        assert self.pm.commit_changes()
        assert not self._reached_remote(self.mine.head.commit.hexsha)

        self.pm.PUSH_RETRY_INTERVAL_SECONDS = 0
        with open(os.path.join(self.mine_path, "edit-2.md"), "w") as f:
            f.write("edit\n")
        assert self.pm.commit_changes()
        assert self._reached_remote(self.mine.head.commit.hexsha)

    def _stall_messages_for_backlog_age(self, caplog, age_seconds):
        self._remote_rejects_pushes()
        when = f"@{int(time.time() - age_seconds)} +0000"
        self._commit_mine("aged.md", GIT_COMMITTER_DATE=when, GIT_AUTHOR_DATE=when)
        with caplog.at_level(logging.INFO, logger="persistence"):
            self.pm._push_to_remote(self.repo_key, self.mine)
        return [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith("Push stalled")
        ]

    def test_backlog_under_warning_age_logs_no_stall(self, caplog):
        assert self._stall_messages_for_backlog_age(caplog, 4 * 60) == []

    def test_backlog_past_warning_age_warns(self, caplog):
        [(level, message)] = self._stall_messages_for_backlog_age(caplog, 6 * 60)
        assert level == logging.WARNING
        assert message.startswith("Push stalled for relay/folder: 1 commits unpushed")

    def test_backlog_past_error_age_logs_error(self, caplog):
        [(level, message)] = self._stall_messages_for_backlog_age(caplog, 31 * 60)
        assert level == logging.ERROR
        assert message.startswith("Push stalled over 30 minutes for relay/folder:")

    def test_configure_remote_repairs_obvious_missing_upstream(self):
        self._unset_upstream(self.mine)

        assert self.pm.configure_git_remote("relay", "folder", self.remote_path, "origin")

        tracking_branch = self.mine.active_branch.tracking_branch()
        assert tracking_branch is not None
        assert tracking_branch.name == "origin/main"

    def test_rejected_push_recovers_after_missing_upstream_repair(self):
        self._unset_upstream(self.mine)

        # The remote moves while this repo has no branch.main.* tracking config.
        # Because origin/main exists locally, the connector can repair the config,
        # rebase onto the refreshed remote, and push without force.
        self._other_pushes("racing-without-upstream.md")
        with open(os.path.join(self.mine_path, "mine-without-upstream.md"), "w") as f:
            f.write("mine\n")
        self.mine.git.add(A=True)
        self.mine.index.commit("stale-parent commit without upstream")

        self.pm._push_to_remote(self.repo_key, self.mine)

        assert self.mine.active_branch.tracking_branch().name == "origin/main"
        assert self._reached_remote(
            self.mine.head.commit.hexsha
        ), "missing-upstream push was not repaired and recovered"

    def test_missing_upstream_repair_fetches_remote_branch_first(self):
        self._unset_upstream(self.mine)
        self.mine.git.update_ref("-d", "refs/remotes/origin/main")

        with pytest.raises(git.exc.BadName):
            self.mine.commit("origin/main")

        with open(os.path.join(self.mine_path, "mine-after-deleted-ref.md"), "w") as f:
            f.write("mine\n")
        self.mine.git.add(A=True)
        self.mine.index.commit("local commit without remote tracking ref")

        self.pm._push_to_remote(self.repo_key, self.mine)

        assert self.mine.active_branch.tracking_branch().name == "origin/main"
        assert self._reached_remote(
            self.mine.head.commit.hexsha
        ), "missing remote-tracking ref was not fetched before upstream repair"

    def test_push_and_verify_raises_on_rejection(self):
        self._other_pushes("racing.md")
        with open(os.path.join(self.mine_path, "mine.md"), "w") as f:
            f.write("mine\n")
        self.mine.git.add(A=True)
        self.mine.index.commit("stale-parent commit")

        with pytest.raises(git.exc.GitCommandError, match="rejected"):
            self.pm._push_and_verify(self.mine.remotes.origin)

    def test_detached_head_reattaches_to_configured_branch_without_losing_commit(self):
        original_main = self.mine.heads.main.commit
        self.mine.git.checkout("--detach")
        with open(os.path.join(self.mine_path, "detached.md"), "w") as f:
            f.write("detached work\n")
        self.mine.git.add(A=True)
        detached_commit = self.mine.index.commit("detached work")
        assert self.mine.head.is_detached

        branch = self.pm._ensure_configured_branch(self.repo_key, self.mine)

        assert not self.mine.head.is_detached
        assert self.mine.active_branch.name == "main"
        assert branch.commit == detached_commit
        assert self.mine.head.commit == detached_commit
        recovery = self.mine.heads[f"recovery/main-{original_main.hexsha[:12]}"]
        assert recovery.commit == original_main

    def test_commit_cycle_recovers_detached_head_and_pushes(self):
        self.mine.git.checkout("--detach")
        with open(os.path.join(self.mine_path, "detached-sync.md"), "w") as f:
            f.write("detached work\n")

        assert self.pm.commit_changes()

        assert not self.mine.head.is_detached
        assert self.mine.active_branch.name == "main"
        assert self._reached_remote(self.mine.head.commit.hexsha)
