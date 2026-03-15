from iflow2api.proxy import build_upstream_body_preview


def test_build_upstream_body_preview_collapses_whitespace() -> None:
    raw = "  {\n  \"error\":   \"bad\" \n}\t"

    assert build_upstream_body_preview(raw) == '{ "error": "bad" }'


def test_build_upstream_body_preview_truncates_to_limit() -> None:
    raw = "x" * 20

    assert build_upstream_body_preview(raw, limit=8) == "xxxxxxxx"
