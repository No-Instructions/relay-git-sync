#!/usr/bin/env python3

import os
import tempfile
import shutil
import json
import pytest
import git
from unittest.mock import patch, MagicMock, mock_open
from persistence import PersistenceManager, SSHKeyManager
from s3rn import S3RemoteFolder, S3RemoteDocument, S3RemoteCanvas, S3RemoteFile


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
        
        with pytest.raises(ValueError, match="contains directory traversal"):
            self.persistence._sanitize_path("..\\evil.txt", self.base_dir)
    
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
        self.persistence = PersistenceManager(self.temp_dir)
        self.relay_id = "test-relay-123"
        
        # Initialize test data
        self.persistence.document_hashes[self.relay_id] = {
            "doc-123": "hash123",
            "canvas-456": "hash456"
        }
        self.persistence.filemeta_folders[self.relay_id] = {
            "folder-789": {
                "/test.md": {"id": "doc-123", "type": "document"},
                "/canvas.canvas": {"id": "canvas-456", "type": "canvas"}
            }
        }
        self.persistence.local_file_state[self.relay_id] = {
            "folder-789": {
                "/test.md": {"doc_id": "doc-123", "type": "document", "hash": "hash123"},
                "/canvas.canvas": {"doc_id": "canvas-456", "type": "canvas", "hash": "hash456"}
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
        self.persistence.document_hashes[self.relay_id]["compound-id-with-many-dashes-12345"] = "hash999"
        self.persistence.local_file_state[self.relay_id]["folder-789"]["/compound.md"] = {
            "doc_id": "compound-id-with-many-dashes-12345", 
            "type": "document"
        }
        
        self.persistence._build_resource_index(self.relay_id)
        
        # Compound ID should not be in index
        assert "compound-id-with-many-dashes-12345" not in self.persistence.resource_index[self.relay_id]
        
        # Regular IDs should still be there
        assert "doc-123" in self.persistence.resource_index[self.relay_id]


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
        
        result_path = self.persistence.write_file_content(document_resource, "/test.md", content, file_hash)
        
        # Check file was written
        assert os.path.exists(result_path)
        with open(result_path, 'r') as f:
            assert f.read() == content
        
        # Check local state was updated
        state = self.persistence.local_file_state[self.relay_id][self.folder_id]["/test.md"]
        assert state["doc_id"] == "doc-123"
        assert state["hash"] == file_hash
        assert state["type"] == "document"
    
    def test_write_binary_file_content(self):
        """Test writing binary file content"""
        file_resource = S3RemoteFile(self.relay_id, self.folder_id, "file-123")
        binary_content = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
        file_hash = "binaryhash123"
        
        result_path = self.persistence.write_binary_file_content(file_resource, "/image.png", binary_content, file_hash)
        
        # Check file was written
        assert os.path.exists(result_path)
        with open(result_path, 'rb') as f:
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
        
        result_path = self.persistence.write_file_content(document_resource, "/deep/nested/path/test.md", content)
        
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
        with open(new_path, 'r') as f:
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
        folder_state = self.persistence.local_file_state.get(self.relay_id, {}).get(self.folder_id, {})
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
                "/canvas.canvas": {"doc_id": "canvas-789", "type": "canvas"}
            }
        }
        
        # Test finding existing files
        path = self.persistence.find_local_file_by_doc_id(self.relay_id, self.folder_id, "doc-123")
        assert path == "/test1.md"
        
        path = self.persistence.find_local_file_by_doc_id(self.relay_id, self.folder_id, "canvas-789")
        assert path == "/canvas.canvas"
        
        # Test non-existent file
        path = self.persistence.find_local_file_by_doc_id(self.relay_id, self.folder_id, "nonexistent")
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
            self.folder_id: {
                "/test.md": {"doc_id": "doc-123", "type": "document"}
            }
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
        test_hashes = {
            "doc-123": "hash123",
            "doc-456": "hash456"
        }
        self.persistence.document_hashes[self.relay_id] = test_hashes
        
        # Save data
        self.persistence.save_persistent_data(self.relay_id)
        
        # Clear in-memory data
        self.persistence.document_hashes[self.relay_id] = {}
        
        # Load data
        self.persistence.load_persistent_data(self.relay_id)
        
        # Check data was restored
        assert self.persistence.document_hashes[self.relay_id] == test_hashes
    
    def test_save_and_load_filemeta(self):
        """Test saving and loading filemeta"""
        test_filemeta = {
            "folder-789": {
                "/test.md": {"id": "doc-123", "type": "document"}
            }
        }
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
        assert self.relay_id in self.persistence.filemeta_folders
        assert self.relay_id in self.persistence.local_file_state
        assert self.relay_id in self.persistence.resource_index
        
        assert self.persistence.document_hashes[self.relay_id] == {}
        assert self.persistence.filemeta_folders[self.relay_id] == {}
        assert self.persistence.local_file_state[self.relay_id] == {}


@patch('git.Repo')
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
    
    @patch('os.path.exists')
    def test_commit_changes_with_dirty_repo(self, mock_exists, mock_git_repo):
        """Test committing changes when repository is dirty"""
        mock_exists.return_value = True
        
        # Setup mock repo
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = True
        mock_repo.untracked_files = ['new_file.txt']
        
        self.persistence.git_repos[f"{self.relay_id}/{self.folder_id}"] = mock_repo
        
        result = self.persistence.commit_changes()
        
        # Check git operations were called
        mock_repo.git.add.assert_called_once_with(A=True)
        mock_repo.index.commit.assert_called_once()
        assert result is True
    
    @patch('os.path.exists')  
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
        self.temp_dir = tempfile.mkdtemp()
        self.ssh_manager = SSHKeyManager(self.temp_dir)
    
    def teardown_method(self):
        shutil.rmtree(self.temp_dir)
    
    def test_generate_keys_creates_keypair(self):
        """Test SSH key generation creates both private and public keys"""
        self.ssh_manager.generate_keys()
        
        # Check both keys exist
        assert os.path.exists(self.ssh_manager.private_key_path)
        assert os.path.exists(self.ssh_manager.public_key_path)
        
        # Check permissions
        private_stat = os.stat(self.ssh_manager.private_key_path)
        assert oct(private_stat.st_mode)[-3:] == '600'
        
        public_stat = os.stat(self.ssh_manager.public_key_path)
        assert oct(public_stat.st_mode)[-3:] == '644'
    
    def test_ensure_keys_exist_generates_if_missing(self):
        """Test ensure_keys_exist generates keys if they don't exist"""
        assert not os.path.exists(self.ssh_manager.private_key_path)
        
        private_path, public_path = self.ssh_manager.ensure_keys_exist()
        
        assert os.path.exists(private_path)
        assert os.path.exists(public_path)
        assert private_path == self.ssh_manager.private_key_path
        assert public_path == self.ssh_manager.public_key_path
    
    def test_get_public_key_content(self):
        """Test getting public key content"""
        self.ssh_manager.generate_keys()
        
        public_key = self.ssh_manager.get_public_key()
        
        # Check it's a valid SSH public key format
        assert public_key.startswith('ssh-rsa ')
        assert len(public_key.split()) >= 2  # ssh-rsa + key data


if __name__ == "__main__":
    pytest.main([__file__])