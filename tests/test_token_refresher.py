import asyncio
from datetime import datetime

from iflow2api.account_pool import build_iflow_config_from_account
from iflow2api.settings import UpstreamAccount
from iflow2api.token_refresher import OAuthTokenRefresher
import iflow2api.token_refresher as token_refresher_module


class _BaseFakeOAuth:
    def __init__(self, user_info=None, user_info_error: Exception | None = None):
        self._user_info = user_info or {}
        self._user_info_error = user_info_error

    async def refresh_token(self, refresh_token: str) -> dict:
        assert refresh_token == "refresh-old"
        return {
            "access_token": "oauth-access-new",
            "refresh_token": "refresh-new",
            "expires_at": datetime(2026, 3, 20, 12, 0, 0),
        }

    async def get_user_info(self, access_token: str) -> dict:
        assert access_token == "oauth-access-new"
        if self._user_info_error is not None:
            raise self._user_info_error
        return self._user_info


def _make_account(api_key: str = "iflow-api-old") -> UpstreamAccount:
    return UpstreamAccount(
        id="acct-1",
        label="demo@example.com",
        auth_type="oauth-iflow",
        api_key=api_key,
        oauth_access_token="oauth-access-old",
        oauth_refresh_token="refresh-old",
        oauth_expires_at="2026-03-16T12:00:00",
        email="demo@example.com",
    )


def test_oauth_refresh_updates_api_key_from_user_info(monkeypatch):
    refresher = OAuthTokenRefresher(retry_count=1)
    account = _make_account()
    config = build_iflow_config_from_account(account)

    monkeypatch.setattr(
        token_refresher_module,
        "IFlowOAuth",
        lambda: _BaseFakeOAuth(
            user_info={
                "apiKey": "iflow-api-new",
                "email": "new@example.com",
                "phone": "13800000000",
            }
        ),
    )
    monkeypatch.setattr(refresher, "_persist_account", lambda updated_account: True)

    assert asyncio.run(refresher._refresh_token_with_retry(account, config)) is True

    assert config.api_key == "iflow-api-new"
    assert config.oauth_access_token == "oauth-access-new"
    assert config.oauth_refresh_token == "refresh-new"
    assert account.api_key == "iflow-api-new"
    assert account.oauth_access_token == "oauth-access-new"
    assert account.oauth_refresh_token == "refresh-new"
    assert account.email == "new@example.com"
    assert account.phone == "13800000000"


def test_oauth_refresh_keeps_existing_api_key_when_user_info_lookup_fails(monkeypatch):
    refresher = OAuthTokenRefresher(retry_count=1)
    account = _make_account(api_key="iflow-api-still-valid")
    config = build_iflow_config_from_account(account)

    monkeypatch.setattr(
        token_refresher_module,
        "IFlowOAuth",
        lambda: _BaseFakeOAuth(user_info_error=ValueError("boom")),
    )
    monkeypatch.setattr(refresher, "_persist_account", lambda updated_account: True)

    assert asyncio.run(refresher._refresh_token_with_retry(account, config)) is True

    assert config.api_key == "iflow-api-still-valid"
    assert account.api_key == "iflow-api-still-valid"
    assert config.oauth_access_token == "oauth-access-new"
    assert account.oauth_access_token == "oauth-access-new"
    assert account.api_key != account.oauth_access_token


def test_oauth_refresh_fails_if_no_api_key_can_be_determined(monkeypatch):
    refresher = OAuthTokenRefresher(retry_count=1)
    account = _make_account(api_key="")
    config = build_iflow_config_from_account(account)

    monkeypatch.setattr(
        token_refresher_module,
        "IFlowOAuth",
        lambda: _BaseFakeOAuth(user_info_error=ValueError("boom")),
    )
    monkeypatch.setattr(refresher, "_persist_account", lambda updated_account: True)

    assert asyncio.run(refresher._refresh_token_with_retry(account, config)) is False
    assert account.api_key == ""
    assert config.api_key == ""
