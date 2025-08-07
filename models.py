#!/usr/bin/env python3

import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass
from enum import Enum
from s3rn import S3RNType, S3RemoteFolder, S3RemoteDocument, S3RemoteFile, S3RemoteCanvas


class ResourceType(Enum):
    """Resource types as they appear in metadata from the relay server"""
    MARKDOWN = "markdown"
    DOCUMENT = "document"
    CANVAS = "canvas"
    FILE = "file"
    IMAGE = "image"
    PDF = "pdf"
    AUDIO = "audio"
    VIDEO = "video"
    FOLDER = "folder"


class SyncType(Enum):
    FOLDER = "folder"
    DOCUMENT = "document"
    CANVAS = "canvas"
    IMAGE = "image"
    PDF = "pdf"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"


# Type alias for binary file types - these are the sync types that represent binary/file content
from typing import Literal
SyncFileType = Literal[SyncType.IMAGE, SyncType.PDF, SyncType.AUDIO, SyncType.VIDEO, SyncType.FILE]


def get_s3rn_resource_category(resource_type: str) -> str:
    """
    Map resource metadata types to S3RN resource categories.
    
    Args:
        resource_type: The resource type from metadata (e.g., 'markdown', 'canvas', 'image')
        
    Returns:
        S3RN resource category: 'document', 'canvas', 'file', or 'folder'
    """
    if resource_type in [ResourceType.MARKDOWN.value, ResourceType.DOCUMENT.value]:
        return "document"
    elif resource_type == ResourceType.CANVAS.value:
        return "canvas"
    elif resource_type in [ResourceType.FILE.value, ResourceType.IMAGE.value, ResourceType.PDF.value, 
                          ResourceType.AUDIO.value, ResourceType.VIDEO.value]:
        return "file"
    elif resource_type == ResourceType.FOLDER.value:
        return "folder"
    else:
        # Unknown types default to file
        return "file"


def create_document_resource_from_metadata(relay_id: str, folder_id: str, metadata: Dict) -> 'S3RNType':
    """Create appropriate S3RN resource from document metadata"""
    from s3rn import S3RemoteDocument, S3RemoteCanvas, S3RemoteFile
    
    # Extract required fields from metadata
    doc_id = metadata.get("id")
    if not doc_id:
        raise ValueError(f"Missing 'id' field in metadata: {metadata}")
    
    resource_type = metadata.get("type")
    if not resource_type:
        raise ValueError(f"Missing 'type' field in metadata: {metadata}")
    
    # Special handling for folder type
    if resource_type == ResourceType.FOLDER.value:
        raise ValueError(f"Cannot create document resource for folder type. Use folder resource instead")
    
    # Map to S3RN resource category and create appropriate resource
    s3rn_category = get_s3rn_resource_category(resource_type)
    
    if s3rn_category == "document":
        return S3RemoteDocument(relay_id, folder_id, doc_id)
    elif s3rn_category == "canvas":
        return S3RemoteCanvas(relay_id, folder_id, doc_id)
    elif s3rn_category == "file":
        return S3RemoteFile(relay_id, folder_id, doc_id)
    else:
        valid_types = [rt.value for rt in ResourceType]
        raise ValueError(f"Unknown resource type '{resource_type}' (mapped to category '{s3rn_category}'). Expected one of: {', '.join(valid_types)}")


class OperationType(Enum):
    CREATE = "create"
    UPDATE = "update"
    RENAME = "rename"
    DELETE = "delete"
    NOOP = "noop"


@dataclass
class SyncOperation:
    type: OperationType
    path: str
    folder_resource: S3RemoteFolder
    document_resource: Optional[S3RNType] = None  # S3RemoteDocument, S3RemoteFile, etc.
    from_path: Optional[str] = None
    to_path: Optional[str] = None
    content: Optional[str] = None
    metadata: Optional[Dict] = None
    completed: bool = False
    error: Optional[str] = None


@dataclass
class FileMetadata:
    id: str
    path: str
    type: str
    hash: Optional[str] = None
    size: Optional[int] = None
    modified: Optional[float] = None


@dataclass
class SyncRequest:
    resource: S3RNType  # Can be folder, document, etc.
    timestamp: datetime


@dataclass
class SyncResult:
    resource: S3RNType
    operations: List[SyncOperation]
    success: bool
    error: Optional[str] = None


class SyncState:
    def __init__(self):
        self.is_syncing = False
        self.sync_requested_during_sync = False
        self.sync_lock = threading.Lock()
        self.pending_operations: List[SyncOperation] = []
        self.completed_operations: List[SyncOperation] = []
        self.has_changes = False
        self.last_git_commit = time.time()
        self.folder_sync_locks = {}  # Per-folder sync locks