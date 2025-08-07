#!/usr/bin/env python3

import os
import tempfile
import pytest
from persistence import PersistenceManager


class TestPathSanitization:
    """Test path sanitization in the persistence layer"""
    
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir
    
    @pytest.fixture
    def persistence_manager(self, temp_dir):
        """Create a PersistenceManager instance for testing"""
        return PersistenceManager(temp_dir)
    
    def test_sanitize_path_valid_paths(self, persistence_manager, temp_dir):
        """Test that valid paths are sanitized correctly"""
        base_dir = os.path.join(temp_dir, "test_folder")
        os.makedirs(base_dir, exist_ok=True)
        
        # Test simple filename
        result = persistence_manager._sanitize_path("test.txt", base_dir)
        expected = os.path.join(base_dir, "test.txt")
        assert result == os.path.abspath(expected)
        
        # Test path with subdirectory
        result = persistence_manager._sanitize_path("subdir/test.txt", base_dir)
        expected = os.path.join(base_dir, "subdir", "test.txt")
        assert result == os.path.abspath(expected)
        
        # Test path with leading slash (should be stripped)
        result = persistence_manager._sanitize_path("/test.txt", base_dir)
        expected = os.path.join(base_dir, "test.txt")
        assert result == os.path.abspath(expected)
        
        # Test path with multiple leading slashes
        result = persistence_manager._sanitize_path("///subdir/test.txt", base_dir)
        expected = os.path.join(base_dir, "subdir", "test.txt")
        assert result == os.path.abspath(expected)
    
    def test_sanitize_path_directory_traversal_attacks(self, persistence_manager, temp_dir):
        """Test that directory traversal attacks are blocked"""
        base_dir = os.path.join(temp_dir, "test_folder")
        os.makedirs(base_dir, exist_ok=True)
        
        # Test basic directory traversal
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager._sanitize_path("../etc/passwd", base_dir)
        
        # Test multiple directory traversals
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager._sanitize_path("../../../etc/passwd", base_dir)
        
        # Test directory traversal in subdirectory
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager._sanitize_path("subdir/../../etc/passwd", base_dir)
        
        # Test directory traversal with leading slash
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager._sanitize_path("/../etc/passwd", base_dir)
        
        # Test mixed valid and invalid components
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager._sanitize_path("valid/../../invalid", base_dir)
    
    def test_sanitize_path_edge_cases(self, persistence_manager, temp_dir):
        """Test edge cases for path sanitization"""
        base_dir = os.path.join(temp_dir, "test_folder")
        os.makedirs(base_dir, exist_ok=True)
        
        # Test empty path
        with pytest.raises(ValueError, match="Empty path provided"):
            persistence_manager._sanitize_path("", base_dir)
        
        # Test None path
        with pytest.raises(ValueError, match="Empty path provided"):
            persistence_manager._sanitize_path(None, base_dir)
        
        # Test path that would resolve to base directory - this should be blocked
        # because "." resolves to the base directory itself, not a file within it
        with pytest.raises(ValueError, match="attempts to escape base directory"):
            persistence_manager._sanitize_path(".", base_dir)
    
    def test_write_file_content_sanitization(self, persistence_manager, temp_dir):
        """Test that write_file_content respects path sanitization"""
        from s3rn import S3RemoteDocument
        
        relay_id = "test-relay-123"
        folder_id = "test-folder-456"
        doc_id = "test-doc-789"
        
        # Create the expected folder structure
        folder_path = os.path.join(temp_dir, "repos", relay_id, folder_id)
        os.makedirs(folder_path, exist_ok=True)
        
        document_resource = S3RemoteDocument(relay_id, folder_id, doc_id)
        
        # Test valid file write
        result = persistence_manager.write_file_content(
            document_resource, "test.txt", "test content"
        )
        assert os.path.exists(result)
        with open(result, 'r') as f:
            assert f.read() == "test content"
        
        # Test directory traversal attack is blocked
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager.write_file_content(
                document_resource, "../../../etc/passwd", "malicious content"
            )
        
        # Ensure malicious file was not created
        malicious_path = os.path.join(temp_dir, "etc", "passwd")
        assert not os.path.exists(malicious_path)
    
    def test_move_file_sanitization(self, persistence_manager, temp_dir):
        """Test that move_file respects path sanitization"""
        from s3rn import S3RemoteDocument
        
        relay_id = "test-relay-123"
        folder_id = "test-folder-456"
        doc_id = "test-doc-789"
        
        # Create the expected folder structure
        folder_path = os.path.join(temp_dir, "repos", relay_id, folder_id)
        os.makedirs(folder_path, exist_ok=True)
        
        document_resource = S3RemoteDocument(relay_id, folder_id, doc_id)
        
        # Create a test file to move
        test_file = os.path.join(folder_path, "test.txt")
        with open(test_file, 'w') as f:
            f.write("test content")
        
        # Test valid move
        old_path, new_path = persistence_manager.move_file(
            document_resource, "test.txt", "moved.txt"
        )
        assert not os.path.exists(old_path)
        assert os.path.exists(new_path)
        
        # Test directory traversal attack in from_path
        with open(test_file, 'w') as f:  # Recreate test file
            f.write("test content")
        
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager.move_file(
                document_resource, "../../../etc/passwd", "safe.txt"
            )
        
        # Test directory traversal attack in to_path
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager.move_file(
                document_resource, "test.txt", "../../../etc/passwd"
            )
    
    def test_delete_file_sanitization(self, persistence_manager, temp_dir):
        """Test that delete_file respects path sanitization"""
        from s3rn import S3RemoteFolder
        
        relay_id = "test-relay-123"
        folder_id = "test-folder-456"
        
        # Create the expected folder structure
        folder_path = os.path.join(temp_dir, "repos", relay_id, folder_id)
        os.makedirs(folder_path, exist_ok=True)
        
        folder_resource = S3RemoteFolder(relay_id, folder_id)
        
        # Create a test file to delete
        test_file = os.path.join(folder_path, "test.txt")
        with open(test_file, 'w') as f:
            f.write("test content")
        
        # Test valid deletion
        result = persistence_manager.delete_file(folder_resource, "test.txt")
        assert not os.path.exists(test_file)
        
        # Test directory traversal attack is blocked
        # Create a file outside the sandbox to ensure it's not deleted
        outside_dir = os.path.join(temp_dir, "outside")
        os.makedirs(outside_dir, exist_ok=True)
        outside_file = os.path.join(outside_dir, "important.txt")
        with open(outside_file, 'w') as f:
            f.write("important data")
        
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager.delete_file(folder_resource, "../outside/important.txt")
        
        # Ensure the file outside sandbox was not deleted
        assert os.path.exists(outside_file)
    
    def test_create_directory_sanitization(self, persistence_manager, temp_dir):
        """Test that create_directory respects path sanitization"""
        from s3rn import S3RemoteFolder
        
        relay_id = "test-relay-123"
        folder_id = "test-folder-456"
        
        # Create the expected base folder structure
        base_folder_path = os.path.join(temp_dir, "repos", relay_id, folder_id)
        os.makedirs(base_folder_path, exist_ok=True)
        
        folder_resource = S3RemoteFolder(relay_id, folder_id)
        
        # Test valid directory creation
        result = persistence_manager.create_directory(folder_resource, "subdir")
        expected_path = os.path.join(base_folder_path, "subdir")
        assert result == expected_path
        assert os.path.exists(expected_path)
        assert os.path.isdir(expected_path)
        
        # Test directory traversal attack is blocked
        with pytest.raises(ValueError, match="contains directory traversal sequences"):
            persistence_manager.create_directory(folder_resource, "../../../tmp/malicious")
        
        # Ensure malicious directory was not created
        malicious_path = os.path.join(temp_dir, "tmp", "malicious")
        assert not os.path.exists(malicious_path)