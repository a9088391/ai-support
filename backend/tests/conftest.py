from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from backend.credentials import DEMO_PASSWORDS_NAME, write_private
from backend.database import Settings, initialize

USER_PASSWORD = "A" * 43
SUPPORT_PASSWORD = "B" * 43

TEST_DSN = "host=db dbname=ai_support_test user=ai_support_test password=disposable-fixture-only"


@pytest.fixture
# 先辨識本案專用 PostgreSQL，再允許任何 schema 或測試資料寫入。
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    demo_file = tmp_path / DEMO_PASSWORDS_NAME
    current = Settings(tmp_path / "api-key", TEST_DSN, demo_file)

    # 帳號、資料庫、版本及伺服器標記都吻合才能清理這個可拋棄的測試庫。
    with current.connect() as conn:
        identity = conn.execute(
            """
            SELECT current_database(), current_user, current_setting('ai_support.test_fixture', true),
                   current_setting('server_version_num')::int / 10000
            """
        ).fetchone()
    if identity != ("ai_support_test", "ai_support_test", "disposable-ticket01", 18):
        pytest.fail("拒絕使用非本案專用的 PostgreSQL 18 測試資料庫。")

    # 每案從空白業務資料開始，CLI 走與 HTTP 同一組設定。
    initialize(current)
    with current.connect() as conn:
        conn.execute(
            "TRUNCATE activation, model_requests, daily_model_quota, "
            "login_sessions, demo_accounts, login_rate_limits"
        )
        conn.execute("INSERT INTO activation DEFAULT VALUES")
    write_private(
        demo_file, f"user={USER_PASSWORD}\nsupport={SUPPORT_PASSWORD}\n", replace=False
    )

    # 只替換環境讀取，保留 CLI、啟用、PostgreSQL、真實 SDK 與傳輸邊界。
    def fixture_configuration() -> Settings:
        return current

    monkeypatch.setattr(
        Settings, "from_environment", staticmethod(fixture_configuration)
    )
    return current


@pytest.fixture
# PostgreSQL 和 API 端點使用真實實作，只替換外部網路回覆。
def mocked_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[list[httpx.Request], list[Callable[[], httpx.Response]]]]:
    requests: list[httpx.Request] = []
    responses: list[Callable[[], httpx.Response]] = []

    # 每次發送必須是預期的 HTTPS Interactions POST，且只有排定的回覆可用。
    def send(_: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.url.path == "/v1/interactions"
        requests.append(request)
        return responses.pop(0)()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    yield requests, responses
