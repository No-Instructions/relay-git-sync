"""Parse optional folder read credentials at runtime entry points."""

import json


def parse_folder_tokens(value: str) -> dict[str, str]:
    if not value:
        return {}

    try:
        tokens = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "RELAY_FOLDER_TOKENS must be a JSON object of folder IDs to tokens"
        ) from exc

    if not isinstance(tokens, dict) or any(
        not isinstance(folder_id, str) or not folder_id or not isinstance(token, str) or not token
        for folder_id, token in tokens.items()
    ):
        raise ValueError(
            "RELAY_FOLDER_TOKENS must map nonempty folder IDs to nonempty token strings"
        )

    return tokens
