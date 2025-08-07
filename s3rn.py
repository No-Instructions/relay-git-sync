#!/usr/bin/env python3

import re
from typing import Union, Protocol
from abc import ABC


UUID = str


class ResourceInterface(Protocol):
    """Protocol defining the interface all resources must implement"""
    def get_resource_id(self) -> str:
        """Get the main resource identifier"""
        ...
    
    def get_resource_type(self) -> str:
        """Get the resource type as a string"""
        ...


class System3Resource(ABC):
    platform: str = "s3rn"


class S3Product(System3Resource):
    product: str


class S3RelayProduct(S3Product):
    platform: str = "s3rn"
    product: str = "relay"


class S3Relay(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, relay_id: UUID):
        self.relay_id = relay_id


class S3RemoteFolder(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, relay_id: UUID, folder_id: UUID):
        self.relay_id = relay_id
        self.folder_id = folder_id
    
    def __str__(self) -> str:
        return f"S3RemoteFolder(relay:{self.relay_id[:8]}..., folder:{self.folder_id[:8]}...)"
    
    def __repr__(self) -> str:
        return f"S3RemoteFolder(relay_id='{self.relay_id}', folder_id='{self.folder_id}')"
    
    def get_resource_id(self) -> str:
        return self.folder_id
    
    def get_resource_type(self) -> str:
        return "folder"


class S3RemoteDocument(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, relay_id: UUID, folder_id: UUID, document_id: UUID):
        self.relay_id = relay_id
        self.folder_id = folder_id
        self.document_id = document_id
    
    def __str__(self) -> str:
        return f"S3RemoteDocument(relay:{self.relay_id[:8]}..., folder:{self.folder_id[:8]}..., doc:{self.document_id[:8]}...)"
    
    def __repr__(self) -> str:
        return f"S3RemoteDocument(relay_id='{self.relay_id}', folder_id='{self.folder_id}', document_id='{self.document_id}')"
    
    def get_resource_id(self) -> str:
        return self.document_id
    
    def get_resource_type(self) -> str:
        return "document"


class S3RemoteCanvas(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, relay_id: UUID, folder_id: UUID, canvas_id: UUID):
        self.relay_id = relay_id
        self.folder_id = folder_id
        self.canvas_id = canvas_id
    
    def __str__(self) -> str:
        return f"S3RemoteCanvas(relay:{self.relay_id[:8]}..., folder:{self.folder_id[:8]}..., canvas:{self.canvas_id[:8]}...)"
    
    def __repr__(self) -> str:
        return f"S3RemoteCanvas(relay_id='{self.relay_id}', folder_id='{self.folder_id}', canvas_id='{self.canvas_id}')"
    
    def get_resource_id(self) -> str:
        return self.canvas_id
    
    def get_resource_type(self) -> str:
        return "canvas"


class S3RemoteFile(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, relay_id: UUID, folder_id: UUID, file_id: UUID):
        self.relay_id = relay_id
        self.folder_id = folder_id
        self.file_id = file_id
    
    def __str__(self) -> str:
        return f"S3RemoteFile(relay:{self.relay_id[:8]}..., folder:{self.folder_id[:8]}..., file:{self.file_id[:8]}...)"
    
    def __repr__(self) -> str:
        return f"S3RemoteFile(relay_id='{self.relay_id}', folder_id='{self.folder_id}', file_id='{self.file_id}')"
    
    def get_resource_id(self) -> str:
        return self.file_id
    
    def get_resource_type(self) -> str:
        return "file"


class S3RemoteBlob(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, relay_id: UUID, folder_id: UUID, file_id: UUID, 
                 hash: str, content_type: str, content_length: str):
        self.relay_id = relay_id
        self.folder_id = folder_id
        self.file_id = file_id
        self.hash = hash
        self.content_type = content_type
        self.content_length = content_length
    
    def __str__(self) -> str:
        return f"S3RemoteBlob(relay:{self.relay_id[:8]}..., folder:{self.folder_id[:8]}..., file:{self.file_id[:8]}..., {self.content_type}, {self.content_length})"
    
    def __repr__(self) -> str:
        return f"S3RemoteBlob(relay_id='{self.relay_id}', folder_id='{self.folder_id}', file_id='{self.file_id}', hash='{self.hash}', content_type='{self.content_type}', content_length='{self.content_length}')"
    
    def get_resource_id(self) -> str:
        return self.file_id
    
    def get_resource_type(self) -> str:
        return "file"  # Blobs are a type of file


class S3Folder(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, folder_id: UUID):
        self.folder_id = folder_id


class S3Document(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, folder_id: UUID, document_id: UUID):
        self.folder_id = folder_id
        self.document_id = document_id


class S3Canvas(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, folder_id: UUID, canvas_id: UUID):
        self.folder_id = folder_id
        self.canvas_id = canvas_id


class S3File(S3Product):
    platform: str = "s3rn"
    product: str = "relay"

    def __init__(self, folder_id: UUID, file_id: UUID):
        self.folder_id = folder_id
        self.file_id = file_id


S3RNType = Union[
    S3RelayProduct,
    S3Relay,
    S3RemoteFolder,
    S3RemoteDocument,
    S3RemoteCanvas,
    S3RemoteFile,
    S3RemoteBlob
]

# Type alias for resources that implement the ResourceInterface
ResourceType = Union[
    S3RemoteFolder,
    S3RemoteDocument,
    S3RemoteCanvas,
    S3RemoteFile,
    S3RemoteBlob
]


class S3RN:
    @staticmethod
    def validate_uuid(uuid: UUID) -> bool:
        """Validate UUID format"""
        uuid_regex = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            re.IGNORECASE
        )
        return bool(uuid_regex.match(uuid))

    @staticmethod
    def encode(entity: S3RNType) -> str:
        """Encode an S3RN entity to string representation"""
        s3rn = f"{entity.platform}:{entity.product}"

        if hasattr(entity, 'relay_id'):
            if not S3RN.validate_uuid(entity.relay_id):
                raise ValueError("Invalid relay UUID")
            s3rn += f":relay:{entity.relay_id}"

        if hasattr(entity, 'folder_id'):
            if not S3RN.validate_uuid(entity.folder_id):
                raise ValueError("Invalid folder UUID")
            s3rn += f":folder:{entity.folder_id}"

        if hasattr(entity, 'document_id'):
            if not S3RN.validate_uuid(entity.document_id):
                raise ValueError("Invalid document UUID")
            s3rn += f":doc:{entity.document_id}"

        if hasattr(entity, 'canvas_id'):
            if not S3RN.validate_uuid(entity.canvas_id):
                raise ValueError("Invalid canvas UUID")
            s3rn += f":canvas:{entity.canvas_id}"

        if hasattr(entity, 'file_id'):
            if not S3RN.validate_uuid(entity.file_id):
                raise ValueError("Invalid file UUID")
            s3rn += f":file:{entity.file_id}"

            if (hasattr(entity, 'hash') and entity.hash and
                hasattr(entity, 'content_type') and 
                hasattr(entity, 'content_length')):
                s3rn += f":sha256:{entity.hash}"
                s3rn += f":contentType:{entity.content_type}"
                s3rn += f":contentLength:{entity.content_length}"

        return s3rn

    @staticmethod
    def decode(s3rn: str) -> S3RNType:
        """Decode string representation to S3RN entity"""
        parts = s3rn.split(":")
        if len(parts) < 3:
            raise ValueError("Invalid s3rn format")

        # Pad parts list to avoid index errors
        parts.extend([None] * (14 - len(parts)))
        
        (_, product, type0, item0, type1, item1, type2, item2, 
         type3, item3, type4, item4, type5, item5) = parts

        if item0 and not S3RN.validate_uuid(item0):
            raise ValueError("Invalid UUID")
        if item1 and not S3RN.validate_uuid(item1):
            raise ValueError("Invalid UUID")
        if item2 and not S3RN.validate_uuid(item2):
            raise ValueError("Invalid UUID")

        if (product == "relay" and type0 == "relay" and 
            type1 == "folder" and type2 == "doc"):
            return S3RemoteDocument(item0, item1, item2)
        elif (product == "relay" and type0 == "relay" and 
              type1 == "folder" and type2 == "canvas"):
            return S3RemoteCanvas(item0, item1, item2)
        elif (product == "relay" and type0 == "relay" and 
              type1 == "folder" and type2 == "file" and
              type3 == "sha256" and type4 == "contentType" and type5 == "contentLength"):
            return S3RemoteBlob(item0, item1, item2, item3, item4, item5)
        elif (product == "relay" and type0 == "relay" and 
              type1 == "folder" and type2 == "file"):
            return S3RemoteFile(item0, item1, item2)
        elif (product == "relay" and type0 == "relay" and type1 == "folder"):
            return S3RemoteFolder(item0, item1)
        elif (product == "relay" and type0 == "folder" and type1 == "document"):
            return S3Document(item0, item1)
        elif (product == "relay" and type0 == "folder" and type1 == "canvas"):
            return S3Canvas(item0, item1)
        elif (product == "relay" and type0 == "folder"):
            return S3Folder(item0)
        elif (product == "relay" and type0 == "relay"):
            return S3Relay(item0)
        elif type0 is None:
            return S3RelayProduct()

        raise ValueError("Invalid s3rn format for the given product type")

    @staticmethod
    def get_compound_document_id(resource: S3RNType) -> str:
        """Get compound document ID for Y-Sweet communication
        
        WARNING: This method should ONLY be called at IO boundaries (RelayClient, WebhookHandler).
        Internal components should use individual resource IDs to maintain separation of concerns.
        Compound IDs are a Y-Sweet protocol detail that should not leak into business logic.
        """
        if isinstance(resource, S3RemoteFolder):
            return f"{resource.relay_id}-{resource.folder_id}"
        elif isinstance(resource, (S3RemoteDocument, S3RemoteCanvas, S3RemoteFile)):
            return f"{resource.relay_id}-{resource.document_id if hasattr(resource, 'document_id') else resource.canvas_id if hasattr(resource, 'canvas_id') else resource.file_id}"
        else:
            raise ValueError(f"Cannot create compound ID for resource type {type(resource)}")

    @staticmethod
    def get_relay_id(resource: S3RNType) -> str:
        """Extract relay ID from any resource that has one"""
        if hasattr(resource, 'relay_id'):
            return resource.relay_id
        else:
            raise ValueError(f"Resource type {type(resource)} does not have a relay_id")

    @staticmethod
    def get_folder_id(resource: S3RNType) -> str:
        """Extract folder ID from any resource that has one"""
        if hasattr(resource, 'folder_id'):
            return resource.folder_id
        else:
            raise ValueError(f"Resource type {type(resource)} does not have a folder_id")

    @staticmethod
    def get_resource_id(resource: ResourceInterface) -> str:
        """Extract the main resource ID from any resource type"""
        return resource.get_resource_id()

    @staticmethod
    def get_resource_type(resource: ResourceInterface) -> str:
        """Get the resource type as a string"""
        return resource.get_resource_type()