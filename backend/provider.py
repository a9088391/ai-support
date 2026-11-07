import logging
from dataclasses import dataclass
from typing import Literal

import httpx
from google import genai
from google.genai import types
from google.genai._gaos.types.interactions import Interaction
from google.genai._interactions import APIError

MODEL = "gemini-3.5-flash-lite"
PROVIDER = "google-gemini"
Verification = Literal["ready", "invalid"]
Delivery = Literal["sent", "unknown", "unsent"]


@dataclass(frozen=True)
class ProbeOutcome:
    status: Verification | None
    delivery: Delivery


# 只接受 Google 明確的憑證拒絕；一般 403、限流或服務錯誤保留既有狀態。
def credential_rejected(error: APIError) -> bool:
    # 固定的 Google HTTPS 端點回覆 401 時，代表目前憑證無法通過驗證。
    if error.status_code == 401:
        return True

    # Google 也會用 400 或 403 附帶具名 ErrorInfo；依結構化原因辨識。
    if error.status_code in (400, 403):
        match error.body:
            case {"error": {"details": list(details)}}:
                for detail in details:
                    match detail:
                        case {"domain": "googleapis.com", "reason": reason} if (
                            reason
                            in (
                                "API_KEY_INVALID",
                                "API_KEY_EXPIRED",
                                "API_KEY_REVOKED",
                            )
                        ):
                            return True
    return False


# 透過固定供應商做一次最小的 Interactions 請求，只回傳分類，不保留回應文字。
def request_verification(key: str) -> ProbeOutcome:
    # SDK 除錯記錄會包含請求標頭及回應本文；即使環境啟用除錯也抑制這些內容。
    logging.getLogger("google.genai").setLevel(logging.INFO)

    # 固定 SDK 會把 attempts=0 改為 1；非空的 [0] 避免任何 HTTP 狀態被當作可重試。
    # HTTP 層關閉轉址與環境代理；傳輸錯誤由 SDK 直接回傳。
    try:
        with (
            httpx.Client(follow_redirects=False, trust_env=False) as http,
            genai.Client(
                api_key=key,
                vertexai=False,
                http_options=types.HttpOptions(
                    api_version="v1",
                    base_url="https://generativelanguage.googleapis.com",
                    httpx_client=http,
                    timeout=15_000,
                    retry_options=types.HttpRetryOptions(
                        attempts=1, http_status_codes=[0]
                    ),
                ),
            ) as client,
        ):
            # 停用 SDK 自動續跑，且不提供工具或既有互動，以一次短回應驗證模型。
            result = client.interactions.create(
                model=MODEL,
                input="Reply with OK.",
                generation_config={
                    "thinking_level": "minimal",
                    "max_output_tokens": 32,
                },
                store=False,
                stream=False,
                automatic_continuation=False,
            )

    # 有 HTTP 狀態就已送出；沒有狀態時，保留不明紀錄且不退額。
    except APIError as error:
        if credential_rejected(error):
            return ProbeOutcome("invalid", "sent")
        return ProbeOutcome(None, "sent" if error.status_code else "unknown")
    except httpx.HTTPError:
        return ProbeOutcome(None, "unknown")

    # 只有完成且有文字輸出才構成可使用指定模型的證據。
    if (
        isinstance(result, Interaction)
        and result.status == "completed"
        and result.output_text
        and result.output_text.strip()
    ):
        return ProbeOutcome("ready", "sent")
    return ProbeOutcome(None, "sent")
