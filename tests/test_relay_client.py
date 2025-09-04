#!/usr/bin/env python3

import pytest
from relay_client import RelayClient


class TestRelayClientIdExtraction:
    """Test ID extraction methods for backwards compatibility with 2 and 3 UUID formats"""
    
    def test_extract_relay_id_two_uuids(self):
        """Test extracting relay ID from 2-UUID format (backwards compatibility)"""
        doc_id = "12345678-1234-1234-1234-123456789abc-87654321-4321-4321-4321-cba987654321"
        relay_id = RelayClient.extract_relay_id(doc_id)
        assert relay_id == "12345678-1234-1234-1234-123456789abc"
    
    def test_extract_relay_id_three_uuids(self):
        """Test extracting relay ID from 3-UUID format (ignores middle UUID)"""
        doc_id = "12345678-1234-1234-1234-123456789abc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-87654321-4321-4321-4321-cba987654321"
        relay_id = RelayClient.extract_relay_id(doc_id)
        assert relay_id == "12345678-1234-1234-1234-123456789abc"
    
    def test_extract_document_id_two_uuids(self):
        """Test extracting document ID from 2-UUID format (backwards compatibility)"""
        doc_id = "12345678-1234-1234-1234-123456789abc-87654321-4321-4321-4321-cba987654321"
        document_id = RelayClient.extract_document_id(doc_id)
        assert document_id == "87654321-4321-4321-4321-cba987654321"
    
    def test_extract_document_id_three_uuids(self):
        """Test extracting document ID from 3-UUID format (takes last UUID)"""
        doc_id = "12345678-1234-1234-1234-123456789abc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-87654321-4321-4321-4321-cba987654321"
        document_id = RelayClient.extract_document_id(doc_id)
        assert document_id == "87654321-4321-4321-4321-cba987654321"
    
    def test_extract_ids_invalid_format_too_short(self):
        """Test that extraction fails with too few UUID parts"""
        doc_id = "12345678-1234-1234-1234"  # Less than 2 UUIDs
        
        with pytest.raises(ValueError) as exc_info:
            RelayClient.extract_relay_id(doc_id)
        assert "at least 2 UUIDs" in str(exc_info.value)
        
        with pytest.raises(ValueError) as exc_info:
            RelayClient.extract_document_id(doc_id)
        assert "at least 2 UUIDs" in str(exc_info.value)
    
    def test_create_folder_resource_two_uuids(self):
        """Test creating folder resource from 2-UUID compound ID"""
        compound_id = "12345678-1234-1234-1234-123456789abc-87654321-4321-4321-4321-cba987654321"
        folder = RelayClient.create_folder_resource_from_compound_id(compound_id)
        
        assert folder.relay_id == "12345678-1234-1234-1234-123456789abc"
        assert folder.folder_id == "87654321-4321-4321-4321-cba987654321"
    
    def test_create_folder_resource_three_uuids(self):
        """Test creating folder resource from 3-UUID compound ID (ignores middle)"""
        compound_id = "12345678-1234-1234-1234-123456789abc-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-87654321-4321-4321-4321-cba987654321"
        folder = RelayClient.create_folder_resource_from_compound_id(compound_id)
        
        assert folder.relay_id == "12345678-1234-1234-1234-123456789abc"
        assert folder.folder_id == "87654321-4321-4321-4321-cba987654321"
    
    def test_create_folder_resource_invalid_format(self):
        """Test that folder creation fails with invalid UUID counts"""
        # Test with incomplete UUID parts (7 parts instead of 10 or 15)
        compound_id = "12345678-1234-1234-1234-123456789abc-87654321-4321"
        
        with pytest.raises(ValueError) as exc_info:
            RelayClient.create_folder_resource_from_compound_id(compound_id)
        assert "2 or 3 complete UUIDs" in str(exc_info.value)
        
        # Test with 12 parts (not divisible by 5)
        compound_id = "12345678-1234-1234-1234-123456789abc-87654321-4321-4321-4321-cba987654321-extra"
        
        with pytest.raises(ValueError) as exc_info:
            RelayClient.create_folder_resource_from_compound_id(compound_id)
        assert "2 or 3 complete UUIDs" in str(exc_info.value)