"""Per-folder read tokens carrying relay-server's routing channel.

A document relay-server first loads for a request without a channel routes its
events to itself, so folder subscribers miss its edits until it is unloaded.
Reading each folder's documents with a token whose channel claim is that folder
keeps such loads routed to the folder.
"""

import json
from unittest.mock import Mock

import cbor2
from pycrdt import Doc, Text

import cli
from relay_auth import CWT_CLAIM_CHANNEL, CWT_CLAIM_SCOPE, b64url_decode, generate_setup
from relay_client import RelayClient
from s3rn import S3RemoteDocument, S3RemoteFolder

RELAY_ID = "ded4554f-8ab5-4b27-821c-31769eaf52d9"
FOLDER_A = "688ac658-4fcf-486b-81de-220a727cd0d4"
FOLDER_B = "5023331b-762a-4585-a82b-b95cba2ed2a6"
DOC_ID = "f526fa9b-4203-4f77-9909-e3013c146ffc"


def text_update(content: str) -> bytes:
    doc = Doc()
    doc.get("contents", type=Text).insert(0, content)
    return doc.get_update()


def claims(token: str) -> dict:
    cwt = cbor2.loads(b64url_decode(token))
    return cbor2.loads(cwt.value.value[2])


def test_setup_mints_one_read_only_token_per_folder_with_its_channel():
    setup = generate_setup(
        server_url="https://relay.example",
        relay_id=RELAY_ID,
        expires_days=None,
        folder_ids=[FOLDER_A, FOLDER_B],
    )

    assert set(setup.folder_tokens) == {FOLDER_A, FOLDER_B}
    for folder_id, token in setup.folder_tokens.items():
        token_claims = claims(token)
        assert token_claims[CWT_CLAIM_CHANNEL] == f"{RELAY_ID}-{folder_id}"
        assert token_claims[CWT_CLAIM_SCOPE] == f"prefix:{RELAY_ID}-:r"
    assert CWT_CLAIM_CHANNEL not in claims(setup.token.value)


def test_setup_without_folders_mints_no_folder_tokens():
    setup = generate_setup(server_url="https://relay.example", relay_id=RELAY_ID, expires_days=1)

    assert setup.folder_tokens == {}


def test_documents_are_read_with_their_folders_token_and_folders_with_the_main_key():
    client = RelayClient("https://relay.example", "MAIN", {FOLDER_A: "FOLDER-A-TOKEN"})
    client.dm = Mock()
    client.dm.get_doc_as_update.return_value = text_update("hello")

    assert client.fetch_document_content(S3RemoteDocument(RELAY_ID, FOLDER_A, DOC_ID)) == "hello"
    client.dm.get_doc_as_update.assert_called_with(f"{RELAY_ID}-{DOC_ID}", token="FOLDER-A-TOKEN")

    client.fetch_document_content(S3RemoteDocument(RELAY_ID, FOLDER_B, DOC_ID))
    client.dm.get_doc_as_update.assert_called_with(f"{RELAY_ID}-{DOC_ID}")

    client.get_document_structure(S3RemoteFolder(RELAY_ID, FOLDER_A))
    client.dm.get_doc_as_update.assert_called_with(f"{RELAY_ID}-{FOLDER_A}")


def test_cli_setup_with_folder_channels_prints_the_folder_tokens(tmp_path, capsys):
    (tmp_path / "git_connectors.toml").write_text(f"""
[relay]
id = "{RELAY_ID}"
url = "https://relay.example"

[[git_connector]]
shared_folder_id = "{FOLDER_A}"

[[git_connector]]
shared_folder_id = "{FOLDER_B}"
""")

    exit_code = cli.main(["--data-dir", str(tmp_path), "setup", "--folder-channels", "--json"])

    assert exit_code == 0
    env = json.loads(capsys.readouterr().out)["data"]["token"]["env"]
    folder_tokens = json.loads(env["RELAY_FOLDER_TOKENS"])
    assert set(folder_tokens) == {FOLDER_A, FOLDER_B}
    assert claims(folder_tokens[FOLDER_B])[CWT_CLAIM_CHANNEL] == f"{RELAY_ID}-{FOLDER_B}"
