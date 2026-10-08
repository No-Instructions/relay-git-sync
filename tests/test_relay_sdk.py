import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import relay_sdk
from relay_sdk import (
    RelayClient,
    SubdocSubscription,
    decode_message,
    encode_event_subscription,
    encode_query_subdocs,
)
from relay_sdk.protocol import MSG_SUBDOCS, encode_bytes


class FakeResponse:
    def __init__(self, *, content=b"", payload=None, status_code=200):
        self.content = content
        self._payload = payload or {}
        self.status_code = status_code
        self.reason = "OK"
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_document_manager_alias_is_not_exported():
    assert not hasattr(relay_sdk, "DocumentManager")


def test_direct_document_update_endpoint_uses_bearer_token(monkeypatch):
    request = Mock(return_value=FakeResponse(content=b"update"))
    monkeypatch.setattr(relay_sdk.requests, "request", request)

    client = RelayClient("https://auth.system3.dev", token="SERVER")

    assert client.get_doc_as_update("doc-123") == b"update"
    assert request.call_args.args[:2] == (
        "GET",
        "https://auth.system3.dev/d/doc-123/as-update",
    )
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer SERVER"}


def test_file_download_uses_server_token_then_presigned_url(monkeypatch):
    request = Mock(
        return_value=FakeResponse(payload={"downloadUrl": "https://s3.example/file?sig=1"})
    )
    get = Mock(return_value=FakeResponse(content=b"file-bytes"))
    monkeypatch.setattr(relay_sdk.requests, "request", request)
    monkeypatch.setattr(relay_sdk.requests, "get", get)

    client = RelayClient("http://relay.internal", token="SERVER")

    assert client.cas_get("doc-123", "a" * 64) == b"file-bytes"
    assert request.call_args.args[:2] == (
        "GET",
        "http://relay.internal/f/doc-123/download-url",
    )
    assert request.call_args.kwargs["params"]["hash"] == "a" * 64
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer SERVER"}
    assert get.call_args.args[0] == "https://s3.example/file?sig=1"


def test_shared_folder_subdoc_subscription_uses_parent_socket_and_child_guid():
    client = RelayClient("https://relay.example/base", token="SERVER")
    folder = client.shared_folder("relay-id", "folder-id")

    assert folder.subdoc_websocket_url("child-id") == (
        "wss://relay.example/base/d/relay-id-folder-id/ws/relay-id-folder-id?token=SERVER"
    )
    assert folder.subdoc_guid("child-id") == "relay-id-child-id"
    assert folder.subdoc_guid("relay-id-child-id") == "relay-id-child-id"


def test_open_subdoc_subscription_connects_and_sends_initial_frames(monkeypatch):
    sent_frames = []
    fake_ws = SimpleNamespace(
        send_binary=sent_frames.append,
        ping=Mock(),
        close=Mock(),
    )
    fake_websocket = SimpleNamespace(create_connection=Mock(return_value=fake_ws))
    monkeypatch.setitem(sys.modules, "websocket", fake_websocket)

    client = RelayClient("https://relay.example/base", token="SERVER")
    folder = client.shared_folder("relay-id", "folder-id")

    subscription = folder.open_subdoc_subscription(["child-id"], timeout=3)

    fake_websocket.create_connection.assert_called_once_with(
        "wss://relay.example/base/d/relay-id-folder-id/ws/relay-id-folder-id?token=SERVER",
        timeout=3,
    )
    assert subscription.ws is fake_ws
    assert sent_frames == [
        encode_event_subscription(["document.updated"]),
        encode_query_subdocs(["relay-id-child-id"]),
    ]

    subscription.ping()
    fake_ws.ping.assert_called_once_with()


def test_subdoc_subscription_query_pages_large_batches():
    sent_frames = []
    client = RelayClient("https://relay.example", token="SERVER")
    subscription = SubdocSubscription(client, "doc-id", [])
    subscription.ws = SimpleNamespace(send_binary=sent_frames.append)

    guids = [f"subdoc-{index}" for index in range(101)]
    subscription.query(guids)

    assert len(sent_frames) == 2
    assert decode_message(sent_frames[0])["values"] == guids[:100]
    assert decode_message(sent_frames[1])["values"] == guids[100:]


def test_query_subdocs_frame_matches_relay_protocol():
    assert encode_query_subdocs(["subdoc-abc"]).hex() == "07010a737562646f632d616263"

    with pytest.raises(ValueError, match="at least one GUID"):
        encode_query_subdocs([])


def test_decode_subdocs_frame():
    cbor2 = pytest.importorskip("cbor2")
    payload = cbor2.dumps(
        {
            "data": {
                "subdoc-abc": {
                    "snapshot": b"\x01\x02",
                    "last_seen": 123,
                }
            }
        }
    )
    message = decode_message(bytes([MSG_SUBDOCS]) + encode_bytes(payload))

    assert message["type"] == "subdocs"
    assert message["snapshots"]["subdoc-abc"].snapshot == b"\x01\x02"
    assert message["snapshots"]["subdoc-abc"].last_seen == 123


def test_document_read_token_override_does_not_change_default(monkeypatch):
    request = Mock(return_value=FakeResponse(content=b"update"))
    monkeypatch.setattr(relay_sdk.requests, "request", request)
    client = RelayClient("https://relay.example", token="MAIN")
    client.get_doc_as_update("note", token="FOLDER")
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer FOLDER"}
    client.get_doc_as_update("folder")
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer MAIN"}
