from __future__ import annotations

import random
import string
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Union
from urllib.parse import quote, urlencode, urlparse, urlunparse

import requests

from .connection import DocConnection
from .error import YSweetError
from .protocol import (
    MAX_QUERY_SUBDOCS_PER_REQUEST,
    MSG_EVENT,
    MSG_EVENT_SUBSCRIBE,
    MSG_EVENT_UNSUBSCRIBE,
    MSG_QUERY_SUBDOCS,
    MSG_SUBDOCS,
    SubdocSnapshot,
    decode_message,
    decode_subdocs_frame,
    encode_event_subscription,
    encode_event_unsubscription,
    encode_query_subdocs,
)

DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_TOKEN = object()


def _cache_buster() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def _quote_path_part(value: str) -> str:
    return quote(str(value), safe="")


@dataclass(frozen=True)
class FileHistoryEntry:
    hash: str
    size: int
    created_at: int

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "FileHistoryEntry":
        return cls(
            hash=payload["hash"],
            size=int(payload["size"]),
            created_at=int(payload["createdAt"]),
        )


@dataclass(frozen=True)
class DocumentVersionEntry:
    version_id: str
    created_at: int
    is_latest: bool

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "DocumentVersionEntry":
        return cls(
            version_id=payload["versionId"],
            created_at=int(payload["createdAt"]),
            is_latest=bool(payload["isLatest"]),
        )


@dataclass(frozen=True)
class RelayFile:
    client: "RelayClient"
    doc_id: str
    file_hash: Optional[str] = None

    def download_url(self, file_hash: Optional[str] = None) -> str:
        return self.client.get_file_download_url(self.doc_id, file_hash or self._required_hash())

    def download(
        self, file_hash: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT_SECONDS
    ) -> bytes:
        return self.client.download_file(
            self.doc_id,
            file_hash or self._required_hash(),
            timeout=timeout,
        )

    def history(self) -> list[FileHistoryEntry]:
        return self.client.get_file_history(self.doc_id)

    def delete(self, file_hash: Optional[str] = None) -> None:
        target_hash = file_hash or self.file_hash
        if target_hash is None:
            self.client.delete_files(self.doc_id)
        else:
            self.client.delete_file(self.doc_id, target_hash)

    def _required_hash(self) -> str:
        if self.file_hash is None:
            raise ValueError("file_hash is required")
        return self.file_hash


@dataclass
class RelayDocument:
    client: "RelayClient"
    doc_id: str

    def get_update(self) -> bytes:
        return self.client.get_doc_as_update(self.doc_id)

    def update(self, update: bytes) -> None:
        self.client.update_doc(self.doc_id, update)

    def versions(self) -> list[DocumentVersionEntry]:
        return self.client.get_doc_versions(self.doc_id)

    def websocket_url(self, token: Optional[str] = None) -> str:
        return self.client.get_websocket_url(self.doc_id, token=token)

    def subdoc_websocket_url(
        self,
        subdoc_id: Optional[str] = None,
        token: Optional[str] = None,
    ) -> str:
        return self.websocket_url(token=token)

    def file(self, file_hash: Optional[str] = None) -> RelayFile:
        return RelayFile(self.client, self.doc_id, file_hash=file_hash)

    def open_subdoc_subscription(
        self,
        guids: Sequence[str],
        *,
        token: Optional[str] = None,
        event_types: Sequence[str] = ("document.updated",),
        timeout: Optional[float] = None,
    ) -> "SubdocSubscription":
        subscription = SubdocSubscription(
            self.client,
            self.doc_id,
            guids,
            token=token,
            event_types=event_types,
        )
        subscription.connect(timeout=timeout)
        return subscription


class SharedFolder(RelayDocument):
    def __init__(self, client: "RelayClient", relay_id: str, folder_id: str):
        self.relay_id = relay_id
        self.folder_id = folder_id
        super().__init__(client, f"{relay_id}-{folder_id}")

    def child_doc_id(self, resource_id: str) -> str:
        if resource_id.startswith(f"{self.relay_id}-"):
            return resource_id
        return f"{self.relay_id}-{resource_id}"

    def document(self, resource_id: str) -> RelayDocument:
        return RelayDocument(self.client, self.child_doc_id(resource_id))

    def file_resource(
        self,
        resource_id: str,
        file_hash: Optional[str] = None,
    ) -> RelayFile:
        return RelayFile(self.client, self.child_doc_id(resource_id), file_hash=file_hash)

    def subdoc_guid(self, resource_id: str) -> str:
        if resource_id.startswith(f"{self.relay_id}-"):
            return resource_id
        return f"{self.relay_id}-{resource_id}"

    def subdoc_websocket_url(
        self,
        resource_id: Optional[str] = None,
        token: Optional[str] = None,
    ) -> str:
        return self.websocket_url(token=token)

    def open_subdoc_subscription(
        self,
        resource_ids: Sequence[str],
        *,
        token: Optional[str] = None,
        event_types: Sequence[str] = ("document.updated",),
        timeout: Optional[float] = None,
    ) -> "SubdocSubscription":
        guids = [self.subdoc_guid(resource_id) for resource_id in resource_ids]
        return super().open_subdoc_subscription(
            guids,
            token=token,
            event_types=event_types,
            timeout=timeout,
        )


class SubdocSubscription:
    def __init__(
        self,
        client: "RelayClient",
        doc_id: str,
        guids: Sequence[str],
        *,
        token: Optional[str] = None,
        event_types: Sequence[str] = ("document.updated",),
    ):
        self.client = client
        self.doc_id = doc_id
        self.guids = list(guids)
        self.token = token
        self.event_types = list(event_types)
        self.ws = None

    @property
    def url(self) -> str:
        return self.client.get_websocket_url(self.doc_id, token=self.token)

    def connect(self, timeout: Optional[float] = None) -> "SubdocSubscription":
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError(
                "websocket-client is required for SubdocSubscription.connect()"
            ) from exc

        self.ws = websocket.create_connection(self.url, timeout=timeout)
        if self.event_types:
            self.ws.send_binary(encode_event_subscription(self.event_types))
        if self.guids:
            self.query(self.guids)
        return self

    def query(self, guids: Sequence[str]) -> None:
        if self.ws is None:
            raise RuntimeError("subscription is not connected")
        query_guids = list(dict.fromkeys(guid for guid in guids if guid))
        for index in range(0, len(query_guids), MAX_QUERY_SUBDOCS_PER_REQUEST):
            self.ws.send_binary(
                encode_query_subdocs(query_guids[index : index + MAX_QUERY_SUBDOCS_PER_REQUEST])
            )

    def recv(self) -> Dict[str, Any]:
        if self.ws is None:
            raise RuntimeError("subscription is not connected")

        frame = self.ws.recv()
        if isinstance(frame, str):
            frame = frame.encode("utf-8")
        return decode_message(frame)

    def ping(self) -> None:
        if self.ws is None:
            raise RuntimeError("subscription is not connected")
        self.ws.ping()

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None

    def __enter__(self) -> "SubdocSubscription":
        if self.ws is None:
            self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class RelayClient:
    def __init__(self, connection_string: str, token: Optional[str] = None):
        parsed_url = urlparse(connection_string)
        self.token = token or (parsed_url.username and requests.utils.unquote(parsed_url.username))

        scheme = parsed_url.scheme
        if scheme == "ys":
            scheme = "http"
        elif scheme == "yss":
            scheme = "https"

        netloc = parsed_url.netloc.rsplit("@", 1)[-1]
        self.base_url = urlunparse((scheme, netloc, parsed_url.path, "", "", "")).rstrip("/")

    def document(self, doc_id: str) -> RelayDocument:
        return RelayDocument(self, doc_id)

    def shared_folder(self, relay_id: str, folder_id: str) -> SharedFolder:
        return SharedFolder(self, relay_id, folder_id)

    def file(self, doc_id: str, file_hash: Optional[str] = None) -> RelayFile:
        return RelayFile(self, doc_id, file_hash=file_hash)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _auth_headers(self, token: Union[str, None, object] = _DEFAULT_TOKEN) -> Dict[str, str]:
        auth_token = self.token if token is _DEFAULT_TOKEN else token
        return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        json_data: Optional[Dict[str, Any]] = None,
        data: Optional[bytes] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        token: Union[str, None, object] = _DEFAULT_TOKEN,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        cache_bust: bool = True,
    ) -> requests.Response:
        url = self._url(path)
        request_headers = self._auth_headers(token)
        if headers:
            request_headers.update(headers)

        request_params = dict(params or {})
        if cache_bust:
            request_params.setdefault("z", _cache_buster())

        try:
            response = requests.request(
                method,
                url,
                headers=request_headers,
                json=json_data,
                data=data,
                params=request_params,
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            self._raise_request_error(exc, url)

        return response

    def _raise_request_error(self, exc: requests.RequestException, url: str) -> None:
        if isinstance(exc, requests.ConnectionError):
            raise YSweetError({"code": "ServerRefused", "url": url}) from exc

        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            response = exc.response
            message = response.text.strip() or response.reason
            if response.status_code == 401:
                if self.token:
                    raise YSweetError({"code": "InvalidAuthProvided"}) from exc
                raise YSweetError({"code": "NoAuthProvided"}) from exc
            if response.status_code == 404:
                raise YSweetError(
                    {
                        "code": "NotFound",
                        "status": response.status_code,
                        "message": message,
                        "url": url,
                    }
                ) from exc
            raise YSweetError(
                {
                    "code": "ServerError",
                    "status": response.status_code,
                    "message": message,
                    "url": url,
                }
            ) from exc

        raise YSweetError({"code": "Unknown", "message": str(exc)}) from exc

    def _do_request(
        self, path: str, method: str = "GET", data: Optional[Dict[str, Any]] = None
    ) -> requests.Response:
        return self._request(path, method=method, json_data=data)

    def _do_raw_request(
        self, path: str, method: str = "GET", data: Optional[bytes] = None
    ) -> requests.Response:
        return self._request(path, method=method, data=data)

    def check_store(self) -> Dict[str, Union[bool, str]]:
        return self._request("check_store").json()

    def create_doc(self, doc_id: Optional[str] = None) -> Dict[str, str]:
        data = {"docId": doc_id} if doc_id else {}
        return self._request("doc/new", method="POST", json_data=data).json()

    def get_client_token(self, doc_id: Union[str, Dict[str, str]]) -> Dict[str, str]:
        if isinstance(doc_id, dict):
            doc_id = doc_id["docId"]
        return self._request(
            f"doc/{_quote_path_part(doc_id)}/auth", method="POST", json_data={}
        ).json()

    def get_or_create_doc_and_token(self, doc_id: Optional[str] = None) -> Dict[str, str]:
        result = self.create_doc(doc_id)
        return self.get_client_token(result)

    def get_doc_as_update(self, doc_id: str) -> bytes:
        return self._request(f"d/{_quote_path_part(doc_id)}/as-update").content

    def get_attributed_content(self, doc_id: str, root: str = "contents") -> Dict[str, Any]:
        return self._request(
            f"d/{_quote_path_part(doc_id)}/attributed-content", params={"root": root}
        ).json()

    def update_doc(self, doc_id: str, update: bytes) -> None:
        self._request(f"d/{_quote_path_part(doc_id)}/update", method="POST", data=update)

    def get_doc_versions(self, doc_id: str) -> list[DocumentVersionEntry]:
        payload = self._request(f"d/{_quote_path_part(doc_id)}/versions").json()
        return [DocumentVersionEntry.from_json(item) for item in payload.get("versions", [])]

    def get_websocket_url(self, doc_id: str, token: Optional[str] = None) -> str:
        token_value = self.token if token is None else token
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        quoted_doc_id = _quote_path_part(doc_id)
        path = f"{base_path}/d/{quoted_doc_id}/ws/{quoted_doc_id}"
        query = urlencode({"token": token_value}) if token_value else ""
        return urlunparse((scheme, parsed.netloc, path, "", query, ""))

    def get_connection(self, doc_id: str, token: Optional[str] = None) -> DocConnection:
        quoted_doc_id = _quote_path_part(doc_id)
        return DocConnection(
            {
                "baseUrl": f"{self.base_url}/d/{quoted_doc_id}",
                "docId": doc_id,
                "token": self.token if token is None else token,
            }
        )

    def get_file_download_url(self, doc_id: str, file_hash: str) -> str:
        payload = self._request(
            f"f/{_quote_path_part(doc_id)}/download-url",
            params={"hash": file_hash},
        ).json()
        return payload["downloadUrl"]

    def download_file(
        self,
        doc_id: str,
        file_hash: str,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> bytes:
        download_url = self.get_file_download_url(doc_id, file_hash)
        response = requests.get(download_url, timeout=timeout)
        response.raise_for_status()
        return response.content

    def get_file_history(self, doc_id: str) -> list[FileHistoryEntry]:
        payload = self._request(f"f/{_quote_path_part(doc_id)}/history").json()
        return [FileHistoryEntry.from_json(item) for item in payload.get("files", [])]

    def delete_files(self, doc_id: str) -> None:
        self._request(f"f/{_quote_path_part(doc_id)}", method="DELETE")

    def delete_file(self, doc_id: str, file_hash: str) -> None:
        self._request(
            f"f/{_quote_path_part(doc_id)}/{_quote_path_part(file_hash)}",
            method="DELETE",
        )

    def get_file_upload_url(self, doc_id: str, file_token: str) -> str:
        payload = self._request(
            f"f/{_quote_path_part(doc_id)}/upload-url",
            method="POST",
            token=file_token,
        ).json()
        return payload["uploadUrl"]

    def upload_file_with_token(
        self,
        doc_id: str,
        file_token: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        headers = {"Content-Type": content_type} if content_type else None
        self._request(
            f"f/{_quote_path_part(doc_id)}/upload",
            method="PUT",
            data=data,
            params={"token": file_token},
            headers=headers,
            token=None,
            timeout=timeout,
            cache_bust=False,
        )

    def download_file_with_token(
        self,
        doc_id: str,
        file_token: str,
        file_hash: str,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> bytes:
        return self._request(
            f"f/{_quote_path_part(doc_id)}/download",
            params={"token": file_token, "hash": file_hash},
            token=None,
            timeout=timeout,
            cache_bust=False,
        ).content

    def cas_get(
        self,
        doc_id: str,
        file_hash: str,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> bytes:
        return self.download_file(doc_id, file_hash, timeout=timeout)

    def cas_put_with_token(
        self,
        doc_id: str,
        file_token: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.upload_file_with_token(
            doc_id,
            file_token,
            data,
            content_type=content_type,
            timeout=timeout,
        )


RelayError = YSweetError


def get_or_create_doc_and_token(
    connection_string: str, doc_id: Optional[str] = None
) -> Dict[str, str]:
    manager = RelayClient(connection_string)
    return manager.get_or_create_doc_and_token(doc_id)


def get_client_token(connection_string: str, doc_id: Union[str, Dict[str, str]]) -> Dict[str, str]:
    manager = RelayClient(connection_string)
    return manager.get_client_token(doc_id)


def create_doc(connection_string: str, doc_id: Optional[str] = None) -> Dict[str, str]:
    manager = RelayClient(connection_string)
    return manager.create_doc(doc_id)


__all__ = [
    "DocumentVersionEntry",
    "FileHistoryEntry",
    "MAX_QUERY_SUBDOCS_PER_REQUEST",
    "MSG_EVENT",
    "MSG_EVENT_SUBSCRIBE",
    "MSG_EVENT_UNSUBSCRIBE",
    "MSG_QUERY_SUBDOCS",
    "MSG_SUBDOCS",
    "RelayClient",
    "RelayDocument",
    "RelayError",
    "RelayFile",
    "SharedFolder",
    "SubdocSnapshot",
    "SubdocSubscription",
    "YSweetError",
    "create_doc",
    "decode_message",
    "decode_subdocs_frame",
    "encode_event_subscription",
    "encode_event_unsubscription",
    "encode_query_subdocs",
    "get_client_token",
    "get_or_create_doc_and_token",
]
