#!/usr/bin/env python3
"""Resolve the git authors for a file change using the relay server's
attributed-content endpoint (per-span authorship of the doc's current text).

The caller supplies the changed character ranges (computed by git itself -
see persistence._changed_char_ranges; difflib.SequenceMatcher was tried here
and is quadratic on large repetitive files, freezing the whole sync process).
The dominant author of the changed characters gets the commit and any other
contributors become co-authors; deletion-only changes and unattributable
content fall back to the default (bot) identity.
"""

import logging
import re
import traceback
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitAuthor:
    name: str
    email: str


_AUTHOR_RE = re.compile(r"^\s*(?P<name>[^<>]+?)\s*<(?P<email>[^<>@\s]+@[^<>\s]+)>\s*$")


def parse_author(value: str) -> Optional[GitAuthor]:
    """Parse a '"Name <email>"' config value."""
    match = _AUTHOR_RE.match(value)
    if not match:
        return None

    return GitAuthor(name=match.group("name"), email=match.group("email"))


def parse_authors(table: Dict[str, str]) -> Dict[str, GitAuthor]:
    """Parse the [authors] config table (relay user id -> 'Name <email>')."""
    authors = {}
    for user_id, value in table.items():
        author = parse_author(value)
        if author:
            authors[user_id] = author
        else:
            logger.error(f"Ignoring malformed author for {user_id!r}: {value!r}")
    return authors


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def new_side_line_ranges(unified_diff: str) -> List[Tuple[int, int]]:
    """New-side (line_start, line_end) ranges from unified diff hunk headers.

    0-based half-open; deletion-only hunks (new count 0) are omitted.
    """
    ranges = []
    for match in _HUNK_RE.finditer(unified_diff):
        start = int(match.group(1)) - 1
        count = 1 if match.group(2) is None else int(match.group(2))
        if count > 0:
            ranges.append((start, start + count))
    return ranges


def line_ranges_to_char_ranges(
    new: str, line_ranges: List[Tuple[int, int]]
) -> List[Tuple[int, int]]:
    """Convert 0-based half-open line ranges into character ranges of `new`."""
    line_offsets = [0]
    for line in new.splitlines(keepends=True):
        line_offsets.append(line_offsets[-1] + len(line))

    def clamp(line: int) -> int:
        return line_offsets[min(line, len(line_offsets) - 1)]

    return [(clamp(l1), clamp(l2)) for l1, l2 in line_ranges if clamp(l2) > clamp(l1)]


def _changed_chars_by_user(
    spans: List[dict], ranges: List[Tuple[int, int]]
) -> Dict[Optional[str], int]:
    """ranges must be sorted and disjoint (unified diff hunks are); O(spans + ranges)."""
    counts: Dict[Optional[str], int] = {}
    offset = 0
    next_range = 0
    for span in spans:
        span_start, span_end = offset, offset + len(span.get("text", ""))
        offset = span_end
        while next_range < len(ranges) and ranges[next_range][1] <= span_start:
            next_range += 1
        overlap = 0
        j = next_range
        while j < len(ranges) and ranges[j][0] < span_end:
            overlap += min(span_end, ranges[j][1]) - max(span_start, ranges[j][0])
            j += 1
        if overlap:
            user = span.get("user")
            counts[user] = counts.get(user, 0) + overlap
    return counts


def attributing_users(
    new: str, spans: List[dict], changed_ranges: List[Tuple[int, int]]
) -> List[str]:
    """Relay users who authored the changed character ranges, most-changed first.

    Empty means "no attributable author": no changed ranges, spans that don't
    reconstruct `new` (the doc moved on since materialization), or changed
    content whose authors are all unmapped in PUD.
    """
    if "".join(span.get("text", "") for span in spans) != new:
        logger.debug("Attributed spans do not reconstruct file content; skipping attribution")
        return []

    counts = _changed_chars_by_user(spans, changed_ranges)
    counts.pop(None, None)
    return sorted(counts, key=lambda user: (-counts[user], user))


class AuthorResolver:
    """Fetches attributed spans for a doc and resolves the change's git authors.

    fetch_spans is relay_client.fetch_attributed_spans (or equivalent):
    given an S3RN document resource, return the span list or None.
    """

    def __init__(
        self,
        fetch_spans: Callable[[object], Optional[List[dict]]],
        authors: Dict[str, GitAuthor],
    ):
        self.fetch_spans = fetch_spans
        self.authors = authors

    def resolve(
        self, resource: object, new: str, changed_ranges: List[Tuple[int, int]]
    ) -> List[GitAuthor]:
        """Git authors of the change, dominant first. Empty = unattributable."""
        if not self.authors or not changed_ranges:
            return []

        try:
            spans = self.fetch_spans(resource)
        except Exception as e:
            logger.warning(f"Attribution fetch failed for {resource}: {e}")
            logger.debug(traceback.format_exc())
            return []
        if not spans:
            return []

        authors = []
        for user in attributing_users(new, spans, changed_ranges):
            author = self.authors.get(user)
            if author is None:
                logger.info(f"No git author configured for relay user {user}")
            elif author not in authors:
                authors.append(author)
        return authors
