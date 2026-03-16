import asyncio

from iflow2api.oauth import IFlowOAuth


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _CapturingClient:
    def __init__(self):
        self.last_url = ""
        self.last_headers: dict | None = None
        self.last_timeout = None

    async def get(self, url: str, headers: dict | None = None, timeout: float | None = None):
        self.last_url = url
        self.last_headers = headers
        self.last_timeout = timeout
        return _FakeResponse({"success": True, "data": {"apiKey": "iflow-key"}})


def test_get_user_info_url_encodes_access_token(monkeypatch):
    oauth = IFlowOAuth()
    client = _CapturingClient()

    async def _fake_get_client():
        return client

    monkeypatch.setattr(oauth, "_get_client", _fake_get_client)

    result = asyncio.run(oauth.get_user_info("abc+/%?=你好"))

    assert result["apiKey"] == "iflow-key"
    assert (
        client.last_url
        == "https://iflow.cn/api/oauth/getUserInfo?accessToken=abc%2B%2F%25%3F%3D%E4%BD%A0%E5%A5%BD"
    )
