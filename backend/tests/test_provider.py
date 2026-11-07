import json
import logging
from collections.abc import Callable

import httpx
import pytest
from backend.provider import MODEL, request_verification

SUCCESS = {
    "id": "test-interaction",
    "status": "completed",
    "steps": [{"type": "model_output", "content": [{"type": "text", "text": "OK"}]}],
}


@pytest.fixture
# 用 SDK 實際採用的 HTTP 傳輸層接管請求，既能計數也不會連到 Google。
def transport(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[int | str, object], list[httpx.Request]]:
    # 為每次驗證提供新計數；把外部服務結果控制在傳輸邊界。
    def install(code: int | str, body: object = None) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        # 記錄每次真正進入 HTTP transport 的請求，並回傳受控結果。
        def send(_: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
            requests.append(request)

            # 逾時用讀取階段重現，這時無法確認外部服務有沒有收到請求。
            if code == "timeout":
                raise httpx.ReadTimeout("test timeout", request=request)
            if code == "connection":
                raise httpx.ConnectError("test connection", request=request)
            if code == "write":
                raise httpx.WriteError("test write", request=request)

            # 加入轉址標頭，驗證收到 307 時也只停在原請求。
            if code == "malformed":
                return httpx.Response(
                    200,
                    content=b'{"fixture-key"',
                    headers={"content-type": "application/json"},
                )
            assert isinstance(code, int)
            return httpx.Response(
                code,
                json=body,
                headers={"location": "https://example.invalid/redirect"},
            )

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
        return requests

    return install


@pytest.mark.parametrize(
    ("code", "body", "status", "delivery"),
    [
        (200, SUCCESS, "ready", "sent"),
        (200, {"status": "completed", "steps": []}, None, "sent"),
        (
            200,
            {"status": "requires_continuation", "continuation_token": "test"},
            None,
            "sent",
        ),
        (
            400,
            {
                "error": {
                    "details": [
                        {"reason": "API_KEY_INVALID", "domain": "googleapis.com"}
                    ]
                }
            },
            "invalid",
            "sent",
        ),
        (401, {"error": {"status": "UNAUTHENTICATED"}}, "invalid", "sent"),
        (403, {"error": {"status": "PERMISSION_DENIED"}}, None, "sent"),
        (429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, None, "sent"),
        (500, {"error": {"status": "INTERNAL"}}, None, "sent"),
        (307, None, None, "sent"),
        ("timeout", None, None, "unknown"),
        ("connection", None, None, "unknown"),
        ("write", None, None, "unknown"),
        ("malformed", None, None, "sent"),
    ],
)
# 由實際 SDK 序列化與發送，驗證單次請求、拒絕保存及各種故障的分類。
def test_sdk_sends_once(
    transport: Callable[[int | str, object], list[httpx.Request]],
    code: int | str,
    body: object,
    status: str | None,
    delivery: str,
) -> None:
    # 在真實 SDK 呼叫後核對外呼次數及非工具模式的請求本文。
    requests = transport(code, body)
    outcome = request_verification("fixture-key")
    assert (outcome.status, outcome.delivery) == (status, delivery)
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.url.path == "/v1/interactions"
    assert request.method == "POST"
    assert request.headers["x-goog-api-key"] == "fixture-key"
    assert "fixture-key" not in str(request.url)
    payload = json.loads(request.content)
    assert payload["model"] == MODEL
    assert payload["store"] is False
    assert "tools" not in payload


# 用新版 AQ. 憑證驗證 SDK 原樣傳送標頭，且不把憑證放入網址或除錯日誌。
def test_sdk_hides_secret_in_debug_logs(
    transport: Callable[[int | str, object], list[httpx.Request]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 使用測試 Key 和供應商回顯錯誤，捕捉最高詳細程度的記錄。
    key = "AQ.fixture-only-not-a-real-key"
    monkeypatch.setenv("GOOGLE_GENAI_DEBUG", "1")
    caplog.set_level(logging.DEBUG, logger="google.genai")
    requests = transport(429, {"error": {"message": key}})
    assert request_verification(key).delivery == "sent"

    # 在實際 SDK 送出的單一請求核對標頭與網址，並確認日誌安全。
    assert len(requests) == 1
    assert requests[0].headers["x-goog-api-key"] == key
    assert key not in str(requests[0].url)
    assert key not in caplog.text
