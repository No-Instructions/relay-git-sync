from unittest.mock import Mock, patch

import pytest

import cli
from folder_channel_tokens import parse_folder_tokens


@pytest.mark.parametrize("value", ["", "{}"])
def test_unconfigured_tokens(value):
    assert parse_folder_tokens(value) == {}


@pytest.mark.parametrize(
    "value",
    ["[1]", '"secret-value"', '{"folder": 1}', '{"folder": ""}', '{"": "token"}', "not-json"],
)
def test_invalid_tokens_are_rejected_without_echoing_values(value):
    with pytest.raises(ValueError) as exc:
        parse_folder_tokens(value)
    assert "secret-value" not in str(exc.value)


def test_manual_sync_passes_folder_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_FOLDER_TOKENS", '{"folder": "folder-token"}')
    args = Mock(
        relay_server_url="https://relay.example",
        relay_server_api_key="main-token",
        data_dir=str(tmp_path),
        relay_id="relay",
        folder_id=None,
    )
    with (
        patch.object(cli, "RelayClient") as client,
        patch.object(cli, "PersistenceManager"),
        patch.object(cli, "SyncEngine") as engine,
    ):
        engine.return_value.sync_relay_all_folders.return_value = []
        cli.sync_command(args)
    client.assert_called_once_with(
        "https://relay.example", "main-token", {"folder": "folder-token"}
    )
