from attribution import (
    AuthorResolver,
    GitAuthor,
    attributing_users,
    line_ranges_to_char_ranges,
    new_side_line_ranges,
    parse_author,
    parse_authors,
)


def spans(*pairs):
    return [{"text": text, "client_id": 1, "user": user} for text, user in pairs]


def test_parse_author():
    assert parse_author("Ada Lovelace <ada@example.com>") == GitAuthor(
        "Ada Lovelace", "ada@example.com"
    )
    assert parse_author("no email at all") is None
    assert parse_author("<just@email.com>") is None


def test_parse_authors_skips_malformed():
    parsed = parse_authors({"u1": "Ada <ada@example.com>", "u2": "nope"})
    assert list(parsed) == ["u1"]


def test_new_side_line_ranges():
    diff = (
        "diff --git a/f.md b/f.md\n"
        "--- a/f.md\n"
        "+++ b/f.md\n"
        "@@ -1,2 +1,3 @@\n"
        "@@ -10 +12 @@\n"
        "@@ -20,3 +22,0 @@\n"  # deletion-only hunk: omitted
    )
    assert new_side_line_ranges(diff) == [(0, 3), (11, 12)]


def test_line_ranges_to_char_ranges():
    new = "aa\nbbb\ncccc\n"
    assert line_ranges_to_char_ranges(new, [(0, 1), (2, 3)]) == [(0, 3), (7, 12)]
    assert line_ranges_to_char_ranges(new, [(5, 9)]) == []  # past end of file
    assert line_ranges_to_char_ranges("no newline", [(0, 1)]) == [(0, 10)]


def test_attributing_users_single_changed_range():
    new = "hello brave world"
    assert attributing_users(
        new, spans(("hello ", "ada"), ("brave ", "bob"), ("world", "ada")), [(6, 12)]
    ) == ["bob"]


def test_attributing_users_ordered_by_changed_chars():
    new = "aaaaaaaaaa" + "bb"
    assert attributing_users(new, spans(("aaaaaaaaaa", "ada"), ("bb", "bob")), [(0, 12)]) == [
        "ada",
        "bob",
    ]


def test_attributing_users_no_ranges_is_empty():
    assert attributing_users("hello", spans(("hello", "ada")), []) == []


def test_attributing_users_mismatched_spans_is_empty():
    assert attributing_users("actual content", spans(("stale content", "ada")), [(0, 5)]) == []


def test_attributing_users_unmapped_users_is_empty():
    assert attributing_users("xyz", spans(("xyz", None)), [(0, 3)]) == []


def test_attributing_users_many_spans_and_ranges_is_fast():
    n = 20_000
    all_spans = [{"text": "ab", "client_id": 1, "user": f"u{i % 7}"} for i in range(n)]
    ranges = [(i * 4, i * 4 + 2) for i in range(n // 2)]
    users = attributing_users("ab" * n, all_spans, ranges)
    assert len(users) == 7


def test_resolver_maps_users_to_configured_authors():
    ada = GitAuthor("Ada", "ada@example.com")
    resolver = AuthorResolver(lambda resource: spans(("new stuff", "u1")), {"u1": ada})
    assert resolver.resolve(object(), "new stuff", [(0, 9)]) == [ada]


def test_resolver_orders_dominant_first_and_skips_unconfigured():
    ada = GitAuthor("Ada", "ada@example.com")
    bob = GitAuthor("Bob", "bob@example.com")
    resolver = AuthorResolver(
        lambda resource: spans(("bbbbbbbbbb", "u2"), ("aaa", "u1"), ("zz", "u3")),
        {"u1": ada, "u2": bob},
    )
    assert resolver.resolve(object(), "bbbbbbbbbbaaazz", [(0, 15)]) == [bob, ada]


def test_resolver_unconfigured_user_is_empty():
    resolver = AuthorResolver(
        lambda resource: spans(("new stuff", "someone-else")),
        {"u1": GitAuthor("Ada", "ada@example.com")},
    )
    assert resolver.resolve(object(), "new stuff", [(0, 9)]) == []


def test_resolver_no_ranges_never_fetches():
    def boom(resource):
        raise AssertionError("should not fetch")

    resolver = AuthorResolver(boom, {"u1": GitAuthor("Ada", "ada@example.com")})
    assert resolver.resolve(object(), "anything", []) == []


def test_resolver_fetch_failure_is_empty():
    def boom(resource):
        raise RuntimeError("server down")

    resolver = AuthorResolver(boom, {"u1": GitAuthor("Ada", "ada@example.com")})
    assert resolver.resolve(object(), "anything", [(0, 8)]) == []


def test_resolver_without_authors_never_fetches():
    def boom(resource):
        raise AssertionError("should not fetch")

    assert AuthorResolver(boom, {}).resolve(object(), "anything", [(0, 8)]) == []
