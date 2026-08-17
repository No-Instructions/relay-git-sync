#!/usr/bin/env python3

import json
import logging
import traceback
from typing import Any, Dict, Optional

from pycrdt import Doc, Map, Text

from models import ResourceType, get_s3rn_resource_category
from relay_sdk import RelayClient as RelaySDKClient
from s3rn import S3RN, S3RemoteCanvas, S3RemoteDocument, S3RemoteFile, S3RemoteFolder, S3RNType

logger = logging.getLogger(__name__)

# A Y-Doc with an empty state vector has never been written to. The Relay
# plugin (src/merge-hsm/state-vectors.ts isEmptyDoc, BackgroundSync.ts
# downloadByGuid) treats this as "server has the guid registered but no peer
# has uploaded content yet" and defers — it is never authoritative-empty.
# Cross-relay re-shares can legitimately leave docs in this state.
EMPTY_STATE_VECTOR = b"\x00"


def is_unsynced_ydoc(doc: Doc) -> bool:
    return doc.get_state() == EMPTY_STATE_VECTOR


class RelayClient:
    """Application wrapper around the Relay SDK with authentication handling"""

    def __init__(self, relay_server_url: str, relay_server_api_key: Optional[str] = None):
        self.relay_server_url = relay_server_url
        self.relay_server_api_key = relay_server_api_key
        self.dm = self._init_document_manager()

    def _init_document_manager(self) -> RelaySDKClient:
        """Initialize the SDK client with configurable server and authentication"""
        if not self.relay_server_url:
            raise ValueError("Relay server URL is required")

        if self.relay_server_api_key:
            logger.debug(f"Connecting to relay server with API key authentication")
            return RelaySDKClient(self.relay_server_url, token=self.relay_server_api_key)
        else:
            logger.debug(f"Connecting to relay server: {self.relay_server_url}")
            return RelaySDKClient(self.relay_server_url)

    def get_doc_as_update(self, doc_id: str) -> bytes:
        """Get document update from the Relay server"""
        return self.dm.get_doc_as_update(doc_id)

    def fetch_attributed_spans(self, resource: S3RNType) -> Optional[list]:
        """Fetch per-author spans of a doc's current text from the Relay server.

        Requires a relay server with the /d/:doc_id/attributed-content
        endpoint; returns None (attribution falls back to the default
        identity) when the server doesn't support it or the doc has no
        "contents" root.
        """
        compound_doc_id = S3RN.get_compound_document_id(resource)
        try:
            return self.dm.get_attributed_content(compound_doc_id).get("spans")
        except Exception as e:
            logger.debug(f"Attributed content unavailable for {compound_doc_id}: {e}")
            return None

    def fetch_document_content(self, resource: S3RNType) -> Optional[str]:
        """Fetch document content from remote using S3RN resource"""
        try:
            # Construct compound ID for Relay at the boundary
            compound_doc_id = S3RN.get_compound_document_id(resource)
            resource_name = f"{type(resource).__name__}({S3RN.encode(resource)})"
            logger.debug(f"📄 Fetching document: {resource_name}")

            # Get the document as an update
            update = self.dm.get_doc_as_update(compound_doc_id)

            # Create a new Doc object and apply the update
            doc = Doc()
            doc.apply_update(update)

            if is_unsynced_ydoc(doc):
                logger.info(
                    f"Deferring fetch for {resource_name}: guid registered but no content uploaded yet"
                )
                return None

            # Check if it has content. A doc with history but empty text was
            # genuinely emptied by a peer, so "" is authoritative here.
            if "contents" in doc.keys():
                text_content = doc.get("contents", type=Text)
                return str(text_content)

            return None
        except Exception as e:
            logger.error(f"Error fetching document {resource}: {e}")
            logger.error(f"Document fetch traceback: {traceback.format_exc()}")
            return None

    def fetch_canvas_content(self, resource: S3RemoteCanvas) -> Optional[str]:
        """Fetch canvas content from remote and export as JSON string"""
        try:
            # Construct compound ID for Relay at the boundary
            compound_doc_id = S3RN.get_compound_document_id(resource)
            resource_name = f"S3RemoteCanvas({S3RN.encode(resource)})"
            logger.debug(f"🎨 Fetching canvas: {resource_name}")

            # Get the document as an update
            update = self.dm.get_doc_as_update(compound_doc_id)

            # Create a new Doc object and apply the update
            doc = Doc()
            doc.apply_update(update)

            if is_unsynced_ydoc(doc):
                logger.info(
                    f"Deferring fetch for {resource_name}: guid registered but no content uploaded yet"
                )
                return None

            # Export canvas data
            canvas_data = self._export_canvas_data(doc)

            # Convert to JSON string with consistent key ordering
            return json.dumps(canvas_data, indent=2, sort_keys=True)

        except Exception as e:
            logger.error(f"Error fetching canvas {resource}: {e}")
            logger.error(f"Canvas fetch traceback: {traceback.format_exc()}")
            return None

    def _export_canvas_data(self, doc: Doc) -> Dict[str, Any]:
        """Export canvas data from Y.Doc following the TypeScript implementation"""
        canvas_data = {"edges": [], "nodes": []}

        # Export edges
        if "edges" in doc.keys():
            yedges = doc.get("edges", type=Map)
            # Process keys in sorted order for consistency
            for key in sorted(yedges.keys()):
                edge_data = yedges[key]
                if isinstance(edge_data, dict):
                    canvas_data["edges"].append(dict(edge_data))
                else:
                    # Convert Map to dict if needed
                    canvas_data["edges"].append(
                        self._map_to_dict(edge_data) if isinstance(edge_data, Map) else edge_data
                    )

        # Export nodes
        if "nodes" in doc.keys():
            ynodes = doc.get("nodes", type=Map)
            # Process keys in sorted order for consistency
            for key in sorted(ynodes.keys()):
                node_data = ynodes[key]

                # Convert to dict if it's a Map
                if isinstance(node_data, Map):
                    node_dict = self._map_to_dict(node_data)
                elif isinstance(node_data, dict):
                    node_dict = dict(node_data)
                else:
                    node_dict = node_data

                # Get text content for this node if it exists
                node_id = node_dict.get("id")
                if node_id and node_id in doc.keys():
                    ytext = doc.get(node_id, type=Text)
                    text_content = str(ytext) if ytext else node_dict.get("text", "")
                    node_dict["text"] = text_content

                canvas_data["nodes"].append(node_dict)

        # Sort edges and nodes by id for consistent ordering
        canvas_data["edges"].sort(key=lambda x: x.get("id", ""))
        canvas_data["nodes"].sort(key=lambda x: x.get("id", ""))

        return canvas_data

    def fetch_s3_file_content(
        self, resource: S3RemoteFile, file_hash: str, mimetype: str = "application/octet-stream"
    ) -> Optional[bytes]:
        """Fetch S3 file content using server token and presigned URL"""
        try:
            s3rn_encoded = S3RN.encode(resource)
            resource_name = f"S3RemoteFile({s3rn_encoded})"
            logger.debug(f"🗄️ Fetching S3 file: {resource_name}")

            compound_doc_id = S3RN.get_compound_document_id(resource)
            content = self.dm.download_file(compound_doc_id, file_hash, timeout=30)
            logger.debug(f"✅ Downloaded S3 file: {len(content)} bytes")
            return content

        except Exception as e:
            logger.error(f"Error fetching S3 file {resource}: {e}")
            logger.error(f"S3 file fetch traceback: {traceback.format_exc()}")
            return None

    def _get_download_url(self, resource: S3RemoteFile, file_hash: str) -> Optional[str]:
        """Get presigned download URL using server token"""
        try:
            compound_doc_id = S3RN.get_compound_document_id(resource)
            return self.dm.get_file_download_url(compound_doc_id, file_hash)

        except Exception as e:
            logger.error(f"❌ Error getting download URL: {e}")
            logger.error(f"❌ Download URL traceback: {traceback.format_exc()}")
            return None

    def get_document_structure(self, resource: S3RNType) -> tuple[Doc, dict]:
        """Get document structure and parse its contents using S3RN resource

        Returns:
            tuple: (doc_object, parsed_content) where parsed_content contains
                   filemeta_dict if it's a folder, content_str if it's a text document
        """
        try:
            # Construct compound ID for Relay at the boundary
            compound_doc_id = S3RN.get_compound_document_id(resource)

            # Get the document as an update
            update = self.dm.get_doc_as_update(compound_doc_id)

            # Create a new Doc object and apply the update
            doc = Doc()
            doc.apply_update(update)

            parsed_content = {}

            if is_unsynced_ydoc(doc):
                parsed_content["type"] = "unsynced"
                return doc, parsed_content

            # Check if it's a folder document (has "filemeta_v0" key)
            if "filemeta_v0" in doc.keys():
                filemeta_content = doc.get("filemeta_v0", type=Map)
                parsed_content["filemeta"] = self._map_to_dict(filemeta_content)
                parsed_content["type"] = "folder"
            elif "contents" in doc.keys():
                text_content = doc.get("contents", type=Text)
                parsed_content["content"] = str(text_content)
                parsed_content["type"] = "document"
            elif "edges" in doc.keys() and "nodes" in doc.keys():
                # Canvas document with edges and nodes
                canvas_data = self._export_canvas_data(doc)
                parsed_content["content"] = json.dumps(canvas_data, indent=2, sort_keys=True)
                parsed_content["type"] = "canvas"
            else:
                parsed_content["type"] = "unknown"

            return doc, parsed_content

        except Exception as e:
            logger.error(f"Error getting document structure for {resource}: {e}")
            logger.error(f"Document structure traceback: {traceback.format_exc()}")
            raise

    def _map_to_dict(self, map_obj: Map) -> dict:
        """Convert pycrdt Map to Python dictionary with consistent key ordering"""
        result = {}
        # Process keys in sorted order for consistency
        for key in sorted(map_obj.keys()):
            value = map_obj[key]
            if isinstance(value, Map):
                result[key] = self._map_to_dict(value)
            elif isinstance(value, Text):
                result[key] = str(value)
            else:
                result[key] = value
        return result

    @staticmethod
    def extract_relay_id(doc_id: str) -> str:
        """Extract relay_id from document UUID (first UUID in compound ID)

        Supports both 2-UUID format (relay_uuid-doc_uuid) and 3-UUID format
        (relay_uuid-middle_uuid-doc_uuid), always returning the first UUID.
        """
        parts = doc_id.split("-")
        if len(parts) < 10:  # Minimum: two UUIDs (5 parts + 5 parts)
            raise ValueError(
                f"Invalid document ID format: {doc_id}. Expected at least 2 UUIDs in compound ID"
            )
        return "-".join(parts[:5])

    @staticmethod
    def extract_document_id(doc_id: str) -> str:
        """Extract document_id from compound UUID (last UUID in compound ID)

        Supports both 2-UUID format (relay_uuid-doc_uuid) and 3-UUID format
        (relay_uuid-middle_uuid-doc_uuid), always returning the last UUID.
        """
        parts = doc_id.split("-")
        if len(parts) < 10:  # Minimum: two UUIDs (5 parts + 5 parts)
            raise ValueError(
                f"Invalid document ID format: {doc_id}. Expected at least 2 UUIDs in compound ID"
            )
        # For 2 UUIDs (10 parts): return parts[5:10]
        # For 3 UUIDs (15 parts): return parts[10:15]
        # Always take the last 5 parts
        return "-".join(parts[-5:])

    @staticmethod
    def create_folder_resource_from_compound_id(compound_id: str) -> S3RemoteFolder:
        """Create S3RemoteFolder from compound folder document ID

        Supports both 2-UUID and 3-UUID formats, using first and last UUIDs.
        """
        parts = compound_id.split("-")
        if len(parts) < 10 or len(parts) % 5 != 0:  # Must be complete UUIDs
            raise ValueError(
                f"Invalid compound ID format: {compound_id}. Expected 2 or 3 complete UUIDs"
            )
        relay_id = "-".join(parts[:5])
        folder_id = "-".join(parts[-5:])  # Always take the last UUID
        return S3RemoteFolder(relay_id, folder_id)

    def get_doc_object(self, resource: S3RNType) -> Doc:
        """Get Y.Doc object from the Relay server - pure I/O operation"""
        try:
            # Construct compound ID for Relay at the boundary
            compound_doc_id = S3RN.get_compound_document_id(resource)
            resource_name = f"{type(resource).__name__}({S3RN.encode(resource)})"
            logger.debug(f"📄 Fetching raw document: {resource_name}")

            # Get the document as an update
            update = self.dm.get_doc_as_update(compound_doc_id)

            # Create a new Doc object and apply the update
            doc = Doc()
            doc.apply_update(update)

            return doc
        except Exception as e:
            logger.error(f"Error fetching raw document {resource}: {e}")
            logger.error(f"Raw document fetch traceback: {traceback.format_exc()}")
            raise
