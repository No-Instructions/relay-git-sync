import json
import re
import unicodedata

import cbor2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from relay_auth import (
    COSE_ALG_EDDSA,
    COSE_HEADER_ALG,
    COSE_HEADER_KEY_ID,
    COSE_SIGN1_TAG,
    CWT_CLAIM_AUDIENCE,
    CWT_CLAIM_EXPIRATION,
    CWT_CLAIM_ISSUED_AT,
    CWT_CLAIM_ISSUER,
    CWT_CLAIM_SCOPE,
    CWT_TAG,
    b64url_decode,
    generate_setup,
    inspect_token,
    key_id_for_public_key,
    print_setup,
    relay_prefix,
    relay_toml_snippet,
    render_setup_markdown,
    setup_as_json,
    timestamp_text,
    token_info_as_json,
)


def decode_generated_token(token: str):
    cwt = cbor2.loads(b64url_decode(token))
    assert cwt.tag == CWT_TAG
    cose = cwt.value
    assert cose.tag == COSE_SIGN1_TAG
    protected, unprotected, payload, signature = cose.value
    return protected, unprotected, payload, signature


def test_relay_prefix_adds_separator():
    assert relay_prefix("85a06712-af14-47bc-a859-e8106cc786e8") == (
        "85a06712-af14-47bc-a859-e8106cc786e8-"
    )
    assert relay_prefix("relay-") == "relay-"


def test_setup_outputs_keypair_and_relay_config_values():
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=7,
        webhook_url="https://git-sync.example.com/webhooks",
        webhook_secret="webhook-secret",
        now=1_700_000_000,
    )

    assert len(b64url_decode(setup.keypair.private_key)) == 32
    assert len(b64url_decode(setup.keypair.public_key)) == 32
    assert setup.keypair.key_id == key_id_for_public_key(setup.keypair.public_key)
    assert setup.keypair.key_id.startswith("git-sync-")
    assert len(setup.keypair.key_id) == len("git-sync-") + 8

    snippet = relay_toml_snippet(setup.keypair)
    assert f'key_id = "{setup.keypair.key_id}"' in snippet
    assert f'public_key = "{setup.keypair.public_key}"' in snippet
    assert 'allowed_token_types = ["prefix"]' in snippet
    assert setup.webhook.url == "https://git-sync.example.com/webhooks"
    assert setup.webhook.secret == "webhook-secret"
    assert setup.webhook.prefix == "relay-id-"


def test_setup_token_is_read_only_relay_prefix_token_with_expiry():
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=7,
        now=1_700_000_000,
    )

    assert setup.token.scope == "prefix:relay-id-:r"
    assert setup.token.expires_at == 1_700_000_000 + 7 * 24 * 60 * 60
    assert "=" not in setup.token.value

    protected, unprotected, payload, signature = decode_generated_token(setup.token.value)
    assert unprotected == {}
    assert len(signature) == 64

    protected_header = cbor2.loads(protected)
    assert protected_header == {
        COSE_HEADER_ALG: COSE_ALG_EDDSA,
        COSE_HEADER_KEY_ID: setup.keypair.key_id.encode("utf-8"),
    }

    claims = cbor2.loads(payload)
    assert claims[CWT_CLAIM_ISSUER] == "relay-server"
    assert claims[CWT_CLAIM_AUDIENCE] == "https://auth.system3.dev"
    assert claims[CWT_CLAIM_EXPIRATION] == setup.token.expires_at
    assert claims[CWT_CLAIM_ISSUED_AT] == 1_700_000_000
    assert claims[CWT_CLAIM_SCOPE] == "prefix:relay-id-:r"


def test_generated_signature_verifies_with_public_key():
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=7,
        now=1_700_000_000,
    )

    protected, _, payload, signature = decode_generated_token(setup.token.value)
    signature_payload = cbor2.dumps(["Signature1", protected, b"", payload])
    public_key = Ed25519PublicKey.from_public_bytes(b64url_decode(setup.keypair.public_key))

    public_key.verify(signature, signature_payload)


def test_json_output_wraps_public_key_and_token_under_data():
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=7,
        webhook_url="https://git-sync.example.com/webhooks",
        webhook_secret="webhook-secret",
        now=1_700_000_000,
    )

    payload = json.loads(setup_as_json(setup))

    assert set(payload) == {"data"}
    assert payload["data"]["publicKey"]["keyId"] == setup.keypair.key_id
    assert payload["data"]["publicKey"]["value"] == setup.keypair.public_key
    assert "relayToml" not in payload["data"]["publicKey"]
    assert "privateKey" not in payload["data"]
    assert payload["data"]["token"]["value"] == setup.token.value
    assert payload["data"]["token"]["serverUrl"] == "https://auth.system3.dev"
    assert "audience" not in payload["data"]["token"]
    assert payload["data"]["token"]["env"]["RELAY_SERVER_API_KEY"] == setup.token.value
    assert payload["data"]["webhook"]["url"] == "https://git-sync.example.com/webhooks"
    assert payload["data"]["webhook"]["prefix"] == "relay-id-"
    assert payload["data"]["webhook"]["secret"] == "webhook-secret"
    assert payload["data"]["webhook"]["env"]["WEBHOOK_SECRET"] == "webhook-secret"


def test_setup_markdown_template_hides_private_key_and_includes_commands():
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=7,
        webhook_url="https://git-sync.example.com/webhooks",
        webhook_secret="webhook-secret",
        now=1_700_000_000,
    )

    output = render_setup_markdown(setup)

    assert setup.keypair.private_key not in output
    assert "private_key" not in output
    assert "RELAY_SERVER_API_KEY=" in output
    assert "WEBHOOK_SECRET=webhook-secret" in output
    assert 'public_key = "' in output
    assert f'key_id = "{setup.keypair.key_id}"' in output
    assert "[[webhooks]]" in output
    assert 'prefix = "relay-id-"' in output
    assert 'url = "https://git-sync.example.com/webhooks"' in output
    assert 'auth_token = "webhook-secret"' in output
    assert output.startswith("## Relay Git Sync")
    assert "In order to connect to your Relay Server" in output
    assert "scoped to your relay.md server UUID" in output
    assert "public-key cryptography" in output
    assert "### Decoded API key" not in output
    assert "Your API key expires at `2023-11-21T22:13:20Z`." in output
    assert setup.token.value in output


def test_human_output_uses_setup_template(capsys):
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=7,
        now=1_700_000_000,
    )

    print_setup(setup)
    output = capsys.readouterr().out

    assert output == render_setup_markdown(setup)


def test_tty_setup_code_blocks_use_erase_line_background(monkeypatch, capsys):
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=None,
        now=1_700_000_000,
    )
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    print_setup(setup)
    output = capsys.readouterr().out

    assert "\x1b[K" in output
    ansi_pattern = re.compile(r"\x1b\[[0-9;]*m")
    for line in output.splitlines():
        if "\x1b[K" not in line:
            continue
        before_erase = line.split("\x1b[K", 1)[0]
        assert not ansi_pattern.sub("", before_erase).endswith(" ")


def terminal_screen_text(output: str, width: int) -> str:
    """Model terminals that keep the cursor on the last column after a full-width row.

    This matches libvterm; xterm.js handles pending wrap differently. The model
    supports one- and two-cell text, SGR, EL, and a TTY's LF-to-CRLF translation.
    Combining characters and other terminal controls are outside its scope.
    """
    rows = [[" "] * width]
    column = 0
    for part in re.split(r"(\x1b\[[0-9;]*[mK])", output):
        if part.startswith("\x1b["):
            if part.endswith("K"):
                assert part == "\x1b[K"
                # Pending wrap leaves the cursor on the last physical column.
                start = min(column, width - 1)
                if rows[-1][start] == "":
                    start -= 1
                rows[-1][start:] = [" "] * (width - start)
            continue

        for char in part:
            if char == "\n":
                rows.append([" "] * width)
                column = 0
            elif char == "\r":
                column = 0
            else:
                assert char.isprintable()
                assert not unicodedata.combining(char)
                cells = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                assert cells <= width
                if column + cells > width:
                    rows.append([" "] * width)
                    column = 0
                rows[-1][column] = char
                if cells == 2:
                    rows[-1][column + 1] = ""
                column += cells
    return "\n".join("".join(row).rstrip() for row in rows)


@pytest.mark.parametrize(
    ("output", "width", "expected"),
    [
        ("abcd\x1b[K\nef", 4, "abc\nef"),
        ("abcd\nef", 4, "abcd\nef"),
        ("abcdef", 4, "abcd\nef"),
        ("abcd\x1b[31mef", 4, "abcd\nef"),
        ("abcd\rxy\x1b[K", 4, "xy"),
        ("ab界\x1b[K\nx", 4, "ab\nx"),
        ("a界界", 4, "a界\n界"),
        ("界界\x1b[31me", 4, "界界\ne"),
    ],
)
def test_terminal_screen_text(output, width, expected):
    assert terminal_screen_text(output, width=width) == expected


@pytest.mark.parametrize(
    ("code", "expected_rows"),
    [
        pytest.param("abcd", ["abcd"], id="exact-width-final-row"),
        pytest.param("abcdef", ["abcd", "ef"], id="short-final-row"),
        pytest.param("界界界", ["界界", "界"], id="wide-characters"),
    ],
)
def test_tty_setup_preserves_code_rows(monkeypatch, capsys, code, expected_rows):
    monkeypatch.setattr("relay_auth.render_setup_markdown", lambda setup: f"```text\n{code}\n```\n")
    monkeypatch.setenv("COLUMNS", "4")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    print_setup(None)
    output = capsys.readouterr().out

    assert terminal_screen_text(output, width=4).splitlines() == ["", *expected_rows, ""]


@pytest.mark.parametrize("width", [28, 58, 120])
def test_tty_setup_preserves_values_on_screen(monkeypatch, capsys, width):
    setup = generate_setup(
        server_url="https://relay.example.test",
        relay_id="85a06712-af14-47bc-a859-e8106cc786e8",
        expires_days=None,
        now=1_700_000_000,
    )
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    print_setup(setup)
    output = capsys.readouterr().out

    lines = re.sub(r"\x1b\[[0-9;]*[mK]", "", output).splitlines()
    # The key wraps, so its first row fills the terminal width.
    assert len(next(line for line in lines if "RELAY_SERVER_API_KEY=" in line)) == width
    assert setup.token.value in "".join(lines)
    screen = terminal_screen_text(output, width=width).replace("\n", "")
    auth_lines = relay_toml_snippet(setup.keypair).splitlines()
    assert auth_lines[0] in screen
    for line in auth_lines[1:]:
        field, value = line.split(" = ", 1)
        assert re.search(rf"{field}\s*=\s*{re.escape(value)}", screen)
    assert f"RELAY_SERVER_URL={setup.token.server_url}" in screen
    assert f"RELAY_SERVER_API_KEY={setup.token.value}" in screen


def test_inspect_token_returns_human_readable_claims():
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=7,
        now=1_700_000_000,
    )

    info = inspect_token(setup.token.value)

    assert info.key_id == setup.keypair.key_id
    assert info.issuer == "relay-server"
    assert info.server_url == "https://auth.system3.dev"
    assert info.scope == "prefix:relay-id-:r"
    assert info.issued_at == 1_700_000_000
    assert info.expires_at == 1_700_000_000 + 7 * 24 * 60 * 60
    assert timestamp_text(info.expires_at) == "2023-11-21T22:13:20Z"


def test_inspect_json_wraps_token_info_under_data():
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=7,
        now=1_700_000_000,
    )
    info = inspect_token(setup.token.value)

    payload = json.loads(token_info_as_json(info))

    assert set(payload) == {"data"}
    assert payload["data"]["token"]["keyId"] == setup.keypair.key_id
    assert payload["data"]["token"]["serverUrl"] == "https://auth.system3.dev"
    assert "audience" not in payload["data"]["token"]
    assert payload["data"]["token"]["scope"] == "prefix:relay-id-:r"
    assert payload["data"]["token"]["expiresAt"] == setup.token.expires_at
    assert payload["data"]["token"]["expiresAtIso"] == "2023-11-21T22:13:20Z"


def test_setup_without_expires_days_has_no_expiration_claim_or_note():
    setup = generate_setup(
        server_url="https://auth.system3.dev",
        relay_id="relay-id",
        expires_days=None,
        now=1_700_000_000,
    )

    assert setup.token.expires_at is None

    _, _, payload, _ = decode_generated_token(setup.token.value)
    claims = cbor2.loads(payload)
    assert CWT_CLAIM_EXPIRATION not in claims

    output = render_setup_markdown(setup)
    assert "Your API key expires" not in output

    json_payload = json.loads(setup_as_json(setup))
    assert json_payload["data"]["token"]["expiresAt"] is None
