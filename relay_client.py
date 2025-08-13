#!/usr/bin/env python3

import logging
import traceback
import requests
import json
from typing import Optional, Dict, Any
from urllib.parse import urlparse, urlunparse
from y_sweet_sdk import DocumentManager
from pycrdt import Doc, Text, Map
from s3rn import S3RNType, S3RN, S3RemoteFolder, S3RemoteDocument, S3RemoteFile, S3RemoteCanvas
from models import ResourceType, get_s3rn_resource_category

logger = logging.getLogger(__name__)


class RelayClient:
    """Wrapper around Y-Sweet DocumentManager with authentication handling"""

    def __init__(self, relay_server_url: str, relay_server_api_key: Optional[str] = None):
        self.relay_server_url = relay_server_url
        self.relay_server_api_key = relay_server_api_key
        self.dm = self._init_document_manager()

    def _init_document_manager(self) -> DocumentManager:
        """Initialize DocumentManager with configurable server and authentication"""
        if not self.relay_server_url:
            raise ValueError("Relay server URL is required")

        if self.relay_server_api_key:
            # Construct connection string with API key as username
            parsed = urlparse(self.relay_server_url)
            # Replace scheme and add API key as username
            scheme = "ys" if parsed.scheme in ["http", "https"] else parsed.scheme
            connection_string = urlunparse(
                (
                    scheme,
                    f"{self.relay_server_api_key}@{parsed.netloc}",
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
            logger.debug(f"Connecting to relay server with API key authentication")
            return DocumentManager(connection_string)
        else:
            logger.debug(f"Connecting to relay server: {self.relay_server_url}")
            return DocumentManager(self.relay_server_url)

    def get_doc_as_update(self, doc_id: str) -> bytes:
        """Get document update from Y-Sweet server"""
        return self.dm.get_doc_as_update(doc_id)

    def fetch_document_content(self, resource: S3RNType) -> Optional[str]:
        """Fetch document content from remote using S3RN resource"""
        try:
            # Construct compound ID for Y-Sweet at the boundary
            compound_doc_id = S3RN.get_compound_document_id(resource)
            resource_name = f"{type(resource).__name__}({S3RN.encode(resource)})"
            logger.debug(f"📄 Fetching document: {resource_name}")

            # Get the document as an update
            update = self.dm.get_doc_as_update(compound_doc_id)

            # Create a new Doc object and apply the update
            doc = Doc()
            doc.apply_update(update)

            # Check if it has content
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
            # Construct compound ID for Y-Sweet at the boundary
            compound_doc_id = S3RN.get_compound_document_id(resource)
            resource_name = f"S3RemoteCanvas({S3RN.encode(resource)})"
            logger.debug(f"🎨 Fetching canvas: {resource_name}")

            # Get the document as an update
            update = self.dm.get_doc_as_update(compound_doc_id)

            # Create a new Doc object and apply the update
            doc = Doc()
            doc.apply_update(update)

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

            # Get download URL directly using server token
            download_url = self._get_download_url(resource, file_hash)
            if not download_url:
                return None

            # Download file content from presigned URL (don't log full URL - contains signature)
            logger.debug(f"🌐 S3 FILE DOWNLOAD REQUEST")

            download_response = requests.get(download_url, timeout=30)

            logger.debug(
                f"✅ S3 FILE DOWNLOAD RESPONSE: {download_response.status_code} ({len(download_response.content)} bytes)"
            )

            if download_response.status_code != 200:
                logger.error(f"❌ S3 file download failed: {download_response.status_code}")

            download_response.raise_for_status()

            logger.debug(f"✅ Downloaded S3 file: {len(download_response.content)} bytes")
            return download_response.content

        except Exception as e:
            logger.error(f"Error fetching S3 file {resource}: {e}")
            logger.error(f"S3 file fetch traceback: {traceback.format_exc()}")
            return None

    def _get_download_url(self, resource: S3RemoteFile, file_hash: str) -> Optional[str]:
        """Get presigned download URL using server token"""
        try:
            # Extract document ID from resource for the API call
            compound_doc_id = S3RN.get_compound_document_id(resource)
            s3rn_encoded = S3RN.encode(resource)

            # Construct download-url endpoint from relay server URL using correct path
            parsed = urlparse(self.relay_server_url)
            if parsed.scheme == "ys":
                # Convert ys:// to https:// for HTTP requests
                download_url_endpoint = f"https://{parsed.netloc}/f/{compound_doc_id}/download-url"
            else:
                download_url_endpoint = f"{self.relay_server_url}/f/{compound_doc_id}/download-url"

            # Add hash as query parameter
            params = {"hash": file_hash}

            headers = {}
            if self.relay_server_api_key:
                headers["Authorization"] = f"Bearer {self.relay_server_api_key}"

            # Construct full URL with params for logging (no sensitive data)
            full_url = f"{download_url_endpoint}?hash={file_hash}"
            logger.debug(f"🌐 DOWNLOAD-URL REQUEST: GET {full_url}")

            response = requests.get(
                download_url_endpoint, params=params, headers=headers, timeout=10
            )

            logger.debug(f"✅ DOWNLOAD-URL RESPONSE: {response.status_code}")

            if response.status_code == 404:
                logger.error("❌ S3 file not found (404)")
                return None

            try:
                response.raise_for_status()
                response_data = response.json()

                download_url = response_data.get("downloadUrl")
                logger.debug(f"🔗 Got presigned URL")
                return download_url

            except requests.exceptions.RequestException as e:
                logger.error(f"❌ DOWNLOAD-URL REQUEST FAILED: {response.status_code} {e}")
                raise

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
            # Construct compound ID for Y-Sweet at the boundary
            compound_doc_id = S3RN.get_compound_document_id(resource)

            # Get the document as an update
            update = self.dm.get_doc_as_update(compound_doc_id)

            # Create a new Doc object and apply the update
            doc = Doc()
            doc.apply_update(update)

            parsed_content = {}

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
        """Extract relay_id from document UUID (first UUID in compound ID)"""
        parts = doc_id.split("-")
        if len(parts) < 10:  # Should be two UUIDs: 5 parts + 5 parts
            raise ValueError(
                f"Invalid document ID format: {doc_id}. Expected compound UUID: relay_uuid-doc_uuid"
            )
        return "-".join(parts[:5])

    @staticmethod
    def extract_document_id(doc_id: str) -> str:
        """Extract document_id from compound UUID (second UUID in compound ID)"""
        parts = doc_id.split("-")
        if len(parts) < 10:  # Should be two UUIDs: 5 parts + 5 parts
            raise ValueError(
                f"Invalid document ID format: {doc_id}. Expected compound UUID: relay_uuid-doc_uuid"
            )
        return "-".join(parts[5:])

    @staticmethod
    def create_folder_resource_from_compound_id(compound_id: str) -> S3RemoteFolder:
        """Create S3RemoteFolder from compound folder document ID"""
        parts = compound_id.split("-")
        if len(parts) != 10:  # Should be two UUIDs: 5 parts + 5 parts
            raise ValueError(f"Invalid compound ID format: {compound_id}")
        relay_id = "-".join(parts[:5])
        folder_id = "-".join(parts[5:])
        return S3RemoteFolder(relay_id, folder_id)

    def get_doc_object(self, resource: S3RNType) -> Doc:
        """Get Y.Doc object from Y-Sweet server - pure I/O operation"""
        try:
            # Construct compound ID for Y-Sweet at the boundary
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
