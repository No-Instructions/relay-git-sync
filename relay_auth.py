#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cbor2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CWT_TAG = 61
COSE_SIGN1_TAG = 18

COSE_HEADER_ALG = 1
COSE_HEADER_KEY_ID = 4
COSE_ALG_EDDSA = -8

CWT_CLAIM_ISSUER = 1
CWT_CLAIM_AUDIENCE = 3
CWT_CLAIM_EXPIRATION = 4
CWT_CLAIM_ISSUED_AT = 6
CWT_CLAIM_SCOPE = -80201
# relay-server's routing-channel claim. A document first loaded with a token
# carrying it routes its events to that channel (its shared folder); one loaded
# without it routes to itself, and folder subscribers never see its edits
# until the server unloads it.
CWT_CLAIM_CHANNEL = -80202

DEFAULT_ISSUER = "relay-server"
KEY_ID_PREFIX = "git-sync"


def _setup_template_path() -> Path:
    # Running from a checkout (or editable install): templates/ sits next to
    # this module. From a wheel install, [tool.setuptools.data-files] places
    # it under sys.prefix instead of site-packages.
    candidates = [
        Path(__file__).with_name("templates") / "relay_auth_setup.md",
        Path(sys.prefix) / "templates" / "relay_auth_setup.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


SETUP_TEMPLATE = _setup_template_path()


@dataclass(frozen=True)
class RelayKeypair:
    key_id: str
    private_key: str
    public_key: str


@dataclass(frozen=True)
class RelayToken:
    value: str
    scope: str
    server_url: str
    relay_id: str
    issued_at: int
    expires_at: int | None


@dataclass(frozen=True)
class RelayWebhook:
    url: str
    secret: str
    prefix: str


@dataclass(frozen=True)
class RelayAuthSetup:
    keypair: RelayKeypair
    token: RelayToken
    webhook: RelayWebhook | None
    # Shared folder ID -> a token with the same scope whose channel claim is
    # that folder, for reading the folder's documents (RELAY_FOLDER_TOKENS).
    folder_tokens: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenInfo:
    key_id: str | None
    issuer: str | None
    server_url: str | None
    scope: str | None
    issued_at: int | None
    expires_at: int | None


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode((encoded + padding).encode("ascii"))


def raw_private_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def raw_public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def key_id_for_public_key(public_key: str) -> str:
    public_key_bytes = b64url_decode(public_key)
    fingerprint = hashlib.sha256(public_key_bytes).hexdigest()[:8]
    return f"{KEY_ID_PREFIX}-{fingerprint}"


def relay_prefix(relay_id: str) -> str:
    relay_id = relay_id.strip()
    if not relay_id:
        raise ValueError("relay_id is required")
    return relay_id if relay_id.endswith("-") else f"{relay_id}-"


def prefix_scope(relay_id: str) -> str:
    return f"prefix:{relay_prefix(relay_id)}:r"


def generate_webhook_secret() -> str:
    return b64url_encode(secrets.token_bytes(32))


def relay_toml_snippet(keypair: RelayKeypair) -> str:
    return "\n".join(
        [
            "[[auth]]",
            f'key_id = "{keypair.key_id}"',
            f'public_key = "{keypair.public_key}"',
            'allowed_token_types = ["prefix"]',
        ]
    )


def create_cwt_sign1_token(
    private_key: Ed25519PrivateKey,
    *,
    key_id: str,
    server_url: str,
    scope: str,
    issued_at: int,
    expires_at: int | None,
    channel: str | None = None,
) -> str:
    protected = cbor2.dumps(
        {
            COSE_HEADER_ALG: COSE_ALG_EDDSA,
            COSE_HEADER_KEY_ID: key_id.encode("utf-8"),
        }
    )
    claims = {
        CWT_CLAIM_ISSUER: DEFAULT_ISSUER,
        CWT_CLAIM_AUDIENCE: server_url,
        CWT_CLAIM_ISSUED_AT: issued_at,
        CWT_CLAIM_SCOPE: scope,
    }
    if expires_at is not None:
        claims[CWT_CLAIM_EXPIRATION] = expires_at
    if channel is not None:
        claims[CWT_CLAIM_CHANNEL] = channel
    payload = cbor2.dumps(claims)
    signature_payload = cbor2.dumps(["Signature1", protected, b"", payload])
    signature = private_key.sign(signature_payload)
    cose_sign1 = [protected, {}, payload, signature]
    token_cbor = cbor2.dumps(cbor2.CBORTag(CWT_TAG, cbor2.CBORTag(COSE_SIGN1_TAG, cose_sign1)))
    return b64url_encode(token_cbor)


def generate_setup(
    *,
    server_url: str,
    relay_id: str,
    expires_days: int | None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    now: int | None = None,
    folder_ids: list[str] | None = None,
) -> RelayAuthSetup:
    if not server_url:
        raise ValueError("server_url is required")
    if expires_days is not None and expires_days <= 0:
        raise ValueError("expires_days must be positive")

    signing_key = Ed25519PrivateKey.generate()
    public_key = b64url_encode(raw_public_key(signing_key))
    keypair = RelayKeypair(
        key_id=key_id_for_public_key(public_key),
        private_key=b64url_encode(raw_private_key(signing_key)),
        public_key=public_key,
    )

    issued_at = int(time.time()) if now is None else now
    expires_at = issued_at + expires_days * 24 * 60 * 60 if expires_days is not None else None
    scope = prefix_scope(relay_id)
    token = RelayToken(
        value=create_cwt_sign1_token(
            signing_key,
            key_id=keypair.key_id,
            server_url=server_url,
            scope=scope,
            issued_at=issued_at,
            expires_at=expires_at,
        ),
        scope=scope,
        server_url=server_url,
        relay_id=relay_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    folder_tokens = {
        folder_id: create_cwt_sign1_token(
            signing_key,
            key_id=keypair.key_id,
            server_url=server_url,
            scope=scope,
            issued_at=issued_at,
            expires_at=expires_at,
            channel=f"{relay_prefix(relay_id)}{folder_id}",
        )
        for folder_id in folder_ids or []
    }
    webhook = None
    if webhook_url:
        webhook = RelayWebhook(
            url=webhook_url,
            secret=webhook_secret or generate_webhook_secret(),
            prefix=relay_prefix(relay_id),
        )
    return RelayAuthSetup(
        keypair=keypair, token=token, webhook=webhook, folder_tokens=folder_tokens
    )


def setup_as_json(setup: RelayAuthSetup) -> str:
    return json.dumps(
        {
            "data": {
                "publicKey": {
                    "keyId": setup.keypair.key_id,
                    "value": setup.keypair.public_key,
                },
                "token": {
                    "value": setup.token.value,
                    "scope": setup.token.scope,
                    "serverUrl": setup.token.server_url,
                    "relayId": setup.token.relay_id,
                    "issuedAt": setup.token.issued_at,
                    "expiresAt": setup.token.expires_at,
                    "env": {
                        "RELAY_SERVER_URL": setup.token.server_url,
                        "RELAY_SERVER_API_KEY": setup.token.value,
                        **folder_tokens_env(setup),
                    },
                },
                "webhook": webhook_as_json(setup.webhook),
            }
        },
        indent=2,
        sort_keys=True,
    )


def webhook_as_json(webhook: RelayWebhook | None) -> dict | None:
    if webhook is None:
        return None
    return {
        "url": webhook.url,
        "prefix": webhook.prefix,
        "secret": webhook.secret,
        "env": {
            "WEBHOOK_SECRET": webhook.secret,
        },
    }


def inspect_token(token: str) -> TokenInfo:
    raw = b64url_decode(token)
    cwt = cbor2.loads(raw)
    if not isinstance(cwt, cbor2.CBORTag) or cwt.tag != CWT_TAG:
        raise ValueError("token is not a CWT")

    cose = cwt.value
    if not isinstance(cose, cbor2.CBORTag) or cose.tag != COSE_SIGN1_TAG:
        raise ValueError("token is not a COSE_Sign1 CWT")
    if not isinstance(cose.value, list) or len(cose.value) != 4:
        raise ValueError("token has an invalid COSE_Sign1 structure")

    protected = cbor2.loads(cose.value[0])
    claims = cbor2.loads(cose.value[2])
    key_id = protected.get(COSE_HEADER_KEY_ID)
    if isinstance(key_id, bytes):
        key_id = key_id.decode("utf-8")

    return TokenInfo(
        key_id=key_id if isinstance(key_id, str) else None,
        issuer=claims.get(CWT_CLAIM_ISSUER),
        server_url=claims.get(CWT_CLAIM_AUDIENCE),
        scope=claims.get(CWT_CLAIM_SCOPE),
        issued_at=claims.get(CWT_CLAIM_ISSUED_AT),
        expires_at=claims.get(CWT_CLAIM_EXPIRATION),
    )


def timestamp_text(timestamp: int | None) -> str:
    if timestamp is None:
        return "(missing)"
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def token_info_as_json(info: TokenInfo) -> str:
    return json.dumps(
        {
            "data": {
                "token": {
                    "keyId": info.key_id,
                    "issuer": info.issuer,
                    "serverUrl": info.server_url,
                    "scope": info.scope,
                    "issuedAt": info.issued_at,
                    "issuedAtIso": timestamp_text(info.issued_at),
                    "expiresAt": info.expires_at,
                    "expiresAtIso": timestamp_text(info.expires_at),
                }
            }
        },
        indent=2,
        sort_keys=True,
    )


def render_setup_markdown(setup: RelayAuthSetup) -> str:
    return SETUP_TEMPLATE.read_text(encoding="utf-8").format(
        key_id=setup.keypair.key_id,
        public_key=setup.keypair.public_key,
        server_url=setup.token.server_url,
        token=setup.token.value,
        relay_prefix=relay_prefix(setup.token.relay_id),
        scope=setup.token.scope,
        issuer=DEFAULT_ISSUER,
        issued_at=timestamp_text(setup.token.issued_at),
        expires_at=decoded_expires_at(setup.token.expires_at),
        expiry_note=expiry_note(setup.token.expires_at),
        relay_webhook_toml=relay_webhook_toml(setup.webhook),
        git_sync_webhook_env=git_sync_webhook_env(setup.webhook)
        + "".join(f"\n{name}='{value}'" for name, value in folder_tokens_env(setup).items()),
        webhook_note=webhook_note(setup.webhook),
    )


def folder_tokens_env(setup: RelayAuthSetup) -> dict:
    if not setup.folder_tokens:
        return {}
    return {"RELAY_FOLDER_TOKENS": json.dumps(setup.folder_tokens, sort_keys=True)}


def relay_webhook_toml(webhook: RelayWebhook | None) -> str:
    if webhook is None:
        return ""
    return "\n".join(
        [
            "",
            "[[webhooks]]",
            f'prefix = "{webhook.prefix}"',
            f'url = "{webhook.url}"',
            f'auth_token = "{webhook.secret}"',
        ]
    )


def git_sync_webhook_env(webhook: RelayWebhook | None) -> str:
    if webhook is None:
        return ""
    return f"\nWEBHOOK_SECRET={webhook.secret}"


def webhook_note(webhook: RelayWebhook | None) -> str:
    if webhook is None:
        return (
            "This setup uses the outbound Relay listener only. Add `[webhook].url` to "
            "`git_connectors.toml` if this Git Sync deployment has a public webhook URL."
        )
    return "Git Sync will also accept Relay webhook delivery at the configured webhook URL."


def decoded_expires_at(expires_at: int | None) -> str:
    if expires_at is None:
        return "(none)"
    return timestamp_text(expires_at)


def expiry_note(expires_at: int | None) -> str:
    if expires_at is None:
        return ""
    return f"Your API key expires at `{timestamp_text(expires_at)}`."


def print_setup(setup: RelayAuthSetup) -> None:
    markdown = render_setup_markdown(setup)
    if sys.stdout.isatty():
        try:
            from rich.console import Console
            from rich.control import ControlType
            from rich.markdown import CodeBlock, Markdown
            from rich.segment import Segment
            from rich.style import Style
            from rich.syntax import Syntax
        except ImportError:
            pass
        else:

            class SetupCodeBlock(CodeBlock):
                def __rich_console__(self, console, options):
                    code = str(self.text).rstrip()
                    background = "grey11"
                    syntax = Syntax(
                        code,
                        self.lexer_name,
                        theme=self.theme,
                        word_wrap=True,
                        padding=0,
                        background_color=background,
                    )
                    background_style = Style(bgcolor=background)
                    erase_line = Segment(
                        "\x1b[K",
                        background_style,
                        ((ControlType.ERASE_IN_LINE,),),
                    )
                    yield erase_line
                    yield Segment.line()
                    for line in Segment.split_lines(console.render(syntax, options)):
                        while line and line[-1].text.isspace():
                            line.pop()
                        yield from line
                        yield erase_line
                        yield Segment.line()
                    yield erase_line
                    yield Segment.line()

            class SetupMarkdown(Markdown):
                elements = {
                    **Markdown.elements,
                    "fence": SetupCodeBlock,
                    "code_block": SetupCodeBlock,
                }

            Console().print(SetupMarkdown(markdown))
            return
    print(markdown, end="" if markdown.endswith("\n") else "\n")


def print_token_info(info: TokenInfo) -> None:
    print("token:")
    print(f"  key_id: {info.key_id or '(missing)'}")
    print(f"  issuer: {info.issuer or '(missing)'}")
    print(f"  server_url: {info.server_url or '(missing)'}")
    print(f"  scope: {info.scope or '(missing)'}")
    print(f"  issued_at: {timestamp_text(info.issued_at)}")
    print(f"  expires_at: {timestamp_text(info.expires_at)}")
