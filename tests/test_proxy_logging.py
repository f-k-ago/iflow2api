from iflow2api.proxy import build_upstream_body_preview, mask_proxy_url


def test_build_upstream_body_preview_collapses_whitespace() -> None:
    raw = "  {\n  \"error\":   \"bad\" \n}\t"

    assert build_upstream_body_preview(raw) == '{ "error": "bad" }'


def test_build_upstream_body_preview_truncates_to_limit() -> None:
    raw = "x" * 20

    assert build_upstream_body_preview(raw, limit=8) == "xxxxxxxx"


def test_mask_proxy_url_hides_password_but_keeps_route() -> None:
    proxy = "http://user:secret@example.com:8080/path?query=1"

    assert mask_proxy_url(proxy) == "http://user:***@example.com:8080/path?query=1"
