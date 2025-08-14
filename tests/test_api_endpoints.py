#!/usr/bin/env python3

import os
import pytest
import tempfile
from unittest.mock import Mock, patch
from starlette.testclient import TestClient

from web_server import StarletteWebServer
from webhook_handler import WebhookProcessor
from operations_queue import OperationsQueue
from persistence import PersistenceManager, SSHKeyManager


class TestPubkeyEndpoint:
    """Test the public SSH key API endpoint"""

    def setup_method(self):
        """Setup test fixtures"""
        # Create mock dependencies
        self.webhook_processor = Mock(spec=WebhookProcessor)
        self.operations_queue = Mock(spec=OperationsQueue)
        self.webhook_secret = "test_secret"
        
        # Create a temporary directory for persistence manager
        self.temp_dir = tempfile.mkdtemp()
        self.persistence_manager = Mock(spec=PersistenceManager)

    def teardown_method(self):
        """Cleanup test fixtures"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_pubkey_endpoint_with_ssh_key(self):
        """Test pubkey endpoint when SSH key is available"""
        # Mock SSH key manager with a test key
        mock_ssh_manager = Mock(spec=SSHKeyManager)
        test_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyDataHereForTesting"
        mock_ssh_manager.get_public_key.return_value = test_public_key
        
        # Set up persistence manager with SSH key manager
        self.persistence_manager.ssh_key_manager = mock_ssh_manager
        
        # Create web server
        server = StarletteWebServer(
            self.webhook_processor,
            self.operations_queue,
            self.webhook_secret,
            self.persistence_manager
        )
        
        # Create test client
        client = TestClient(server.app)
        
        # Make request to pubkey endpoint
        response = client.get("/api/pubkey")
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert "public_key" in data
        assert "key_type" in data
        assert data["public_key"] == test_public_key
        assert data["key_type"] == "ssh-ed25519"

    def test_pubkey_endpoint_with_rsa_key(self):
        """Test pubkey endpoint with RSA key type detection"""
        # Mock SSH key manager with RSA key
        mock_ssh_manager = Mock(spec=SSHKeyManager)
        test_public_key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQExampleRSAKeyDataHere"
        mock_ssh_manager.get_public_key.return_value = test_public_key
        
        # Set up persistence manager with SSH key manager
        self.persistence_manager.ssh_key_manager = mock_ssh_manager
        
        # Create web server
        server = StarletteWebServer(
            self.webhook_processor,
            self.operations_queue,
            self.webhook_secret,
            self.persistence_manager
        )
        
        # Create test client
        client = TestClient(server.app)
        
        # Make request to pubkey endpoint
        response = client.get("/api/pubkey")
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["public_key"] == test_public_key
        assert data["key_type"] == "ssh-rsa"

    def test_pubkey_endpoint_no_ssh_key(self):
        """Test pubkey endpoint when SSH key is not configured"""
        # Set up persistence manager without SSH key manager
        self.persistence_manager.ssh_key_manager = None
        
        # Create web server
        server = StarletteWebServer(
            self.webhook_processor,
            self.operations_queue,
            self.webhook_secret,
            self.persistence_manager
        )
        
        # Create test client
        client = TestClient(server.app)
        
        # Make request to pubkey endpoint
        response = client.get("/api/pubkey")
        
        # Verify error response
        assert response.status_code == 400
        data = response.json()
        assert "error" in data
        assert "SSH_PRIVATE_KEY environment variable not configured" in data["error"]

    def test_pubkey_endpoint_ssh_key_error(self):
        """Test pubkey endpoint when SSH key manager throws an error"""
        # Mock SSH key manager that throws an error
        mock_ssh_manager = Mock(spec=SSHKeyManager)
        mock_ssh_manager.get_public_key.side_effect = ValueError("Invalid key format")
        
        # Set up persistence manager with SSH key manager
        self.persistence_manager.ssh_key_manager = mock_ssh_manager
        
        # Create web server
        server = StarletteWebServer(
            self.webhook_processor,
            self.operations_queue,
            self.webhook_secret,
            self.persistence_manager
        )
        
        # Create test client
        client = TestClient(server.app)
        
        # Make request to pubkey endpoint
        response = client.get("/api/pubkey")
        
        # Verify error response
        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        assert "Failed to retrieve public key" in data["error"]

    def test_pubkey_endpoint_ecdsa_key_detection(self):
        """Test pubkey endpoint with ECDSA key type detection"""
        # Mock SSH key manager with ECDSA key
        mock_ssh_manager = Mock(spec=SSHKeyManager)
        test_public_key = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYExampleECDSAKey"
        mock_ssh_manager.get_public_key.return_value = test_public_key
        
        # Set up persistence manager with SSH key manager
        self.persistence_manager.ssh_key_manager = mock_ssh_manager
        
        # Create web server
        server = StarletteWebServer(
            self.webhook_processor,
            self.operations_queue,
            self.webhook_secret,
            self.persistence_manager
        )
        
        # Create test client
        client = TestClient(server.app)
        
        # Make request to pubkey endpoint
        response = client.get("/api/pubkey")
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["public_key"] == test_public_key
        assert data["key_type"] == "ecdsa"

    def test_pubkey_endpoint_unknown_key_type(self):
        """Test pubkey endpoint with unknown key type"""
        # Mock SSH key manager with unknown key type
        mock_ssh_manager = Mock(spec=SSHKeyManager)
        test_public_key = "unknown-key-type SomeUnknownKeyData"
        mock_ssh_manager.get_public_key.return_value = test_public_key
        
        # Set up persistence manager with SSH key manager
        self.persistence_manager.ssh_key_manager = mock_ssh_manager
        
        # Create web server
        server = StarletteWebServer(
            self.webhook_processor,
            self.operations_queue,
            self.webhook_secret,
            self.persistence_manager
        )
        
        # Create test client
        client = TestClient(server.app)
        
        # Make request to pubkey endpoint
        response = client.get("/api/pubkey")
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["public_key"] == test_public_key
        assert data["key_type"] == "unknown"


class TestDocumentationEndpoints:
    """Test the documentation endpoints"""

    def setup_method(self):
        """Setup test fixtures"""
        # Create mock dependencies
        self.webhook_processor = Mock(spec=WebhookProcessor)
        self.operations_queue = Mock(spec=OperationsQueue)
        self.webhook_secret = "test_secret"
        self.persistence_manager = Mock(spec=PersistenceManager)
        self.persistence_manager.ssh_key_manager = None

    def test_api_docs_endpoint(self):
        """Test the /docs endpoint returns HTML"""
        # Create web server
        server = StarletteWebServer(
            self.webhook_processor,
            self.operations_queue,
            self.webhook_secret,
            self.persistence_manager
        )
        
        # Create test client
        client = TestClient(server.app)
        
        # Make request to docs endpoint
        response = client.get("/docs")
        
        # Verify response
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        
        # Check that it contains Swagger UI elements
        content = response.text
        assert "swagger-ui" in content
        assert "SwaggerUIBundle" in content
        assert "Relay Git Sync API Documentation" in content
        assert "unpkg.com/swagger-ui-dist" in content  # CDN links

    def test_openapi_spec_endpoint(self):
        """Test the /openapi.yaml endpoint returns YAML spec"""
        # Create web server
        server = StarletteWebServer(
            self.webhook_processor,
            self.operations_queue,
            self.webhook_secret,
            self.persistence_manager
        )
        
        # Create test client
        client = TestClient(server.app)
        
        # Make request to openapi.yaml endpoint
        response = client.get("/openapi.yaml")
        
        # Verify response
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-yaml"
        
        # Check that it contains OpenAPI spec content
        content = response.text
        assert "openapi:" in content
        assert "Relay Git Sync API" in content
        assert "/api/pubkey" in content

    def test_openapi_spec_updates_server_url(self):
        """Test that the OpenAPI spec updates server URL dynamically"""
        # Create web server
        server = StarletteWebServer(
            self.webhook_processor,
            self.operations_queue,
            self.webhook_secret,
            self.persistence_manager
        )
        
        # Create test client with custom base URL
        client = TestClient(server.app, base_url="http://testserver:8080")
        
        # Make request to openapi.yaml endpoint
        response = client.get("/openapi.yaml")
        
        # Verify response
        assert response.status_code == 200
        
        # Check that server URL was updated
        content = response.text
        assert "http://testserver:8080" in content


if __name__ == '__main__':
    pytest.main([__file__, '-v'])