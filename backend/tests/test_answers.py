import asyncio
import json
import multiprocessing
import ssl
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http import HTTPStatus
from multiprocessing.connection import Connection as ProcessPipe
from threading import Barrier, Event
from time import monotonic
from typing import cast
from uuid import UUID, uuid4

import anyio
import httpcore
import httpx
import pytest
from backend import answer_storage, answers, cli, knowledge, sessions
from backend.answer_storage import (
    Access,
    Attempt,
    AttemptWindow,
    Dispatch,
    lock_dispatch,
    lock_running_attempt,
    search_attempt,
)
from backend.app import create_app
from backend.credentials import set_key
from backend.database import Settings, initialize, reserve_request
from backend.tests.conftest import TEST_DSN
from backend.tests.test_activation import success
from backend.tests.test_knowledge import (
    ORIGINAL,
    ORIGINAL_RESULTS,
    import_batch,
    wait_for_search_lock,
)
from backend.tests.test_sessions import (
    CSRF,
    FIXTURE_KEY,
    ORIGIN,
    SUPPORT_FORM,
    USER_FORM,
    ProviderFixture,
    replay_cookie,
)
from backend.tests.test_sessions import clock as clock
from fastapi.testclient import TestClient
from httpcore._backends.auto import AutoBackend
from psycopg import Connection, Cursor
from psycopg.abc import Params
from psycopg.sql import SQL, Composed
from starlette.types import Message, Scope

ANSWERS = "/api/answers"
QUESTION = "一般配送要多久？"
THOUGHT = {"type": "thought", "signature": "Zml4dHVyZS1zaWduYXR1cmU="}
CALL = {
    "type": "function_call",
    "id": "call-delivery",
    "name": "search_knowledge",
    "arguments": {"query": "配送"},
}


@pytest.fixture
# 以 CLI 明確驗證測試 Key，業務外呼由個別案例接管非同步傳輸。
def answer_ready(settings: Settings, mocked_provider: ProviderFixture) -> Settings:
    _, responses = mocked_provider
    set_key(settings.key_file, FIXTURE_KEY)
    responses.append(success)
    assert cli.main(["verify-key"]) == 0
    return settings


# 製作與供應商一致的步驟及狀態，讓實際 SDK 負責解析。
def interaction(status: str, *steps: object) -> httpx.Response:
    return httpx.Response(
        200, json={"id": "answer-fixture", "status": status, "steps": steps}
    )


class ProviderScript:
    # 腳本只取代網路串流，讓 SDK、HTTPX、httpcore、h11 與 trace 真實執行。
    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.responses: list[
            httpx.Response | Callable[[], Awaitable[httpx.Response]]
        ] = []
        self.before_connect: Callable[[], Awaitable[None]] | None = None
        self.before_write: Callable[[], Awaitable[None]] | None = None

    # 連線完成前可控制競爭順序；斷言固定正式端點，永遠不連到外網。
    async def connect(self, host: str, port: int) -> httpcore.AsyncNetworkStream:
        assert (host, port) == ("generativelanguage.googleapis.com", 443)
        if self.before_connect is not None:
            await self.before_connect()
        return FixtureStream(self)


class FixtureStream(httpcore.AsyncNetworkStream):
    # 記錄 HTTP/1.1 實際送入串流的 bytes，回覆等到請求本文完全寫完。
    def __init__(self, script: ProviderScript) -> None:
        self.script = script
        self.pending = bytearray()

    # 首次讀取代表 httpcore 正在等遠端回覆；可在此重現逾時或斷線。
    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del timeout
        self.script.requests.append(bytes(self.pending))
        reply = self.script.responses.pop(0)
        if callable(reply):
            reply = await reply()
        status = HTTPStatus(reply.status_code)
        extra = (
            f"Location: {reply.headers['location']}\r\n"
            if "location" in reply.headers
            else ""
        )
        wire = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(reply.content)}\r\n"
            f"{extra}Connection: close\r\n\r\n"
        ).encode("ascii") + reply.content
        assert len(wire) <= max_bytes
        return wire

    # 允許測試在寫入邊界觀察實際鎖或模擬部分傳輸失敗。
    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del timeout
        if self.script.before_write is not None:
            await self.script.before_write()
        self.pending.extend(buffer)

    # 底層沒有真實 socket；HTTPX 仍走完整資源關閉流程。
    async def aclose(self) -> None:
        return

    # 只以受控串流替代 TLS 網路，實際檢查 SDK 仍使用固定 Google 網域。
    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        assert server_hostname == "generativelanguage.googleapis.com"
        return self


@pytest.fixture
# 接管業務 SDK 的網路後端，原始用戶 HTTP 與測試 CLI 入口照常運作。
def provider_script(monkeypatch: pytest.MonkeyPatch) -> ProviderScript:
    script = ProviderScript()

    # 保留呼叫端指定的端點，並使用每次連線獨立的受控串流。
    async def connect(
        _: AutoBackend, host: str, port: int, **_kwargs: object
    ) -> httpcore.AsyncNetworkStream:
        return await script.connect(host, port)

    monkeypatch.setattr(AutoBackend, "connect_tcp", connect)
    return script


# 生成一個完整文字輸出，讓本地驗證決定可否交付。
def model_output(answer: str | None, citations: list[int]) -> httpx.Response:
    return interaction(
        "completed",
        {
            "type": "model_output",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"answer": answer, "citations": citations}, ensure_ascii=False
                    ),
                }
            ],
        },
    )


# 經同源入口登入；測試用獨立 Cookie 重送時仍會走後端核對。
def login(browser: TestClient, form: dict[str, str] = USER_FORM) -> str:
    response = browser.post("/api/login", headers=CSRF, json=form)
    assert response.status_code == 200
    return cast(str, response.cookies[sessions.COOKIE_NAME])


# 不繞過來源檢查或 Session，透過真實 HTTP 入口提出問題。
def ask(browser: TestClient, question: str = QUESTION) -> httpx.Response:
    return cast(
        httpx.Response, browser.post(ANSWERS, headers=CSRF, json={"question": question})
    )


# 用 HTTP 完成查詢，核對實際 SDK 傳輸及來源快照均保留正確識別。
def test_answer_grounds_citations_in_a_real_tool_result(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    # 來源經 CLI 驗證後匯入 PostgreSQL；提供一個工具步驟及一個有依據的回答。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    provider_script.responses.extend(
        [
            interaction("requires_action", THOUGHT, CALL),
            model_output("一般配送需兩個工作天。", [2]),
        ]
    )

    # 從真實登入入口拿到 Cookie；模型及引用僅從本次工具搜尋取得。
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        assert (
            browser.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
        )
        response = browser.post(ANSWERS, headers=CSRF, json={"question": QUESTION})
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "answered"
        assert payload["answer"] == "一般配送需兩個工作天。"
        assert payload["citations"] == [ORIGINAL_RESULTS["results"][1]]
        assert payload["search_results"] == ORIGINAL_RESULTS["results"]
        assert response.headers["cache-control"] == "no-store"

        # 僅可經當前嘗試識別取回自己的結果，回應不含供應商步驟或簽章。
        readback = browser.get(f"{ANSWERS}/{payload['attempt_id']}")
        assert readback.status_code == 200
        assert readback.json() == payload
        assert "signature" not in response.text and "call-delivery" not in response.text

    # 第二次請求原樣攜帶供應商步驟，並把正確的查詢結果綁回工具呼叫。
    first, second = [
        json.loads(raw.split(b"\r\n\r\n", 1)[1]) for raw in provider_script.requests
    ]
    assert first["store"] is False and second["store"] is False
    assert first["model"] == second["model"] == "gemini-3.5-flash-lite"
    assert "previous_interaction_id" not in second
    original_input = cast(list[object], first["input"])
    continuation = cast(list[dict[str, object]], second["input"])
    assert continuation[:3] == [*original_input, THOUGHT, CALL]
    assert continuation[3]["type"] == "function_result"
    assert continuation[3]["call_id"] == "call-delivery"
    assert "一般配送需兩個工作天。" in str(continuation[3]["result"])


# 追問攜帶完整可信前文，但沒有本次工具結果就不能引用上一題的證據。
def test_follow_up_replays_exact_provider_steps_without_reusing_evidence(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    final = model_output("一般配送需兩個工作天。", [2])
    final_step = final.json()["steps"][0]
    provider_script.responses.extend(
        [
            interaction("requires_action", THOUGHT, CALL),
            interaction("completed", THOUGHT, final_step),
            model_output("沿用上一題的引用。", [2]),
        ]
    )

    # 第二題沿用後端識別；公開回應只提供本題結果與新識別。
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        first = ask(browser).json()
        assert first["status"] == "answered"
        response = browser.post(
            ANSWERS,
            headers=CSRF,
            json={
                "question": "那一般要幾天？",
                "conversation_id": first["conversation_id"],
            },
        )
        assert response.status_code == 200
        second = response.json()
        assert second["conversation_id"] == first["conversation_id"]
        assert second["question_id"] != first["question_id"]
        assert second["attempt_id"] != first["attempt_id"]
        assert second["status"] == "invalid_output"
        assert second["answer"] is None and second["citations"] == []
        assert second["search_results"] == []
        assert "signature" not in response.text and "call-delivery" not in response.text

    # SDK 傳輸保留原順序、原始模型輸出、工具結果與不透明簽章，不使用供應商互動 ID。
    _, continuation, follow_up = [
        json.loads(raw.split(b"\r\n\r\n", 1)[1]) for raw in provider_script.requests
    ]
    assert follow_up["store"] is False and "previous_interaction_id" not in follow_up
    assert follow_up["input"] == [
        *continuation["input"],
        THOUGHT,
        final_step,
        {"type": "user_input", "content": [{"type": "text", "text": "那一般要幾天？"}]},
    ]


# 重試只送已發給頁面的識別，原始問題與可信前文由後端載入。
def retry_form(payload: dict[str, str]) -> dict[str, str]:
    return {
        key: payload[key] for key in ("conversation_id", "question_id", "attempt_id")
    }


# 同題重試不採用失敗步驟或較晚問題，且過時嘗試不能再讀取或重送。
def test_retry_uses_original_question_and_only_earlier_successes(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    provider_script.responses.extend(
        [
            interaction("requires_action", THOUGHT, CALL),
            model_output("一般配送需兩個工作天。", [2]),
            interaction("requires_action", {**CALL, "id": "failed-call"}),
            httpx.Response(502, json={"error": {"status": "UNAVAILABLE"}}),
            interaction("requires_action", {**CALL, "id": "later-call"}),
            model_output("一般配送需兩個工作天。", [2]),
            interaction("requires_action", {**CALL, "id": "retry-call"}),
            model_output("一般配送需兩個工作天。", [2]),
        ]
    )

    # 原問題失敗後仍可送新題，重試原問題時更換嘗試識別而保留原題及對話。
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        first = ask(browser).json()
        conversation = {"conversation_id": first["conversation_id"]}
        for question, expected in (("原題", "unavailable"), ("後來的題目", "answered")):
            response = browser.post(
                ANSWERS, headers=CSRF, json={**conversation, "question": question}
            )
            assert response.status_code == 200
            assert response.json()["status"] == expected
            if expected == "unavailable":
                old = response.json()
        form = retry_form(old)
        response = browser.post(ANSWERS, headers=CSRF, json=form)
        assert response.status_code == 200
        retried = response.json()
        assert retried["status"] == "answered"
        assert retry_form(retried) == {**form, "attempt_id": retried["attempt_id"]}
        assert retried["attempt_id"] != old["attempt_id"]
        assert browser.get(f"{ANSWERS}/{old['attempt_id']}").status_code == 404
        assert browser.get(f"{ANSWERS}/{retried['attempt_id']}").json() == retried
        assert browser.post(ANSWERS, headers=CSRF, json=form).status_code == 409
        assert (
            browser.post(ANSWERS, headers=CSRF, json=retry_form(retried)).status_code
            == 409
        )

    # 相同原始問題及較早成功前文會逐步重現，失敗與較晚步驟沒有進入重試。
    wire = [
        json.loads(raw.split(b"\r\n\r\n", 1)[1]) for raw in provider_script.requests
    ]
    assert len(wire) == 8
    assert wire[6]["input"] == wire[2]["input"]
    assert wire[6]["input"][-1]["content"][0]["text"] == "原題"
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM questions").fetchone() == (3,)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (9,)


# 初始受理後、SDK 寫入前若對話達到絕對期限，不建立預留也不送私人前文。
def test_follow_up_expiring_at_wire_never_reserves_or_sends(
    answer_ready: Settings,
    provider_script: ProviderScript,
    clock: list[datetime],
) -> None:
    provider_script.responses.append(model_output(None, []))
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        first = ask(browser).json()
        with answer_ready.connect() as conn:
            conn.execute(
                "UPDATE conversations SET created_at = %s",
                (clock[0] - timedelta(hours=2) + timedelta(microseconds=1),),
            )

        # 只移動可控制的測試時間，實際連線、查驗及模型預留照常運行。
        async def expire_at_connect() -> None:
            clock[0] += timedelta(microseconds=1)

        provider_script.before_connect = expire_at_connect
        provider_script.responses.append(model_output(None, []))
        denied = browser.post(
            ANSWERS,
            headers=CSRF,
            json={"question": "追問", "conversation_id": first["conversation_id"]},
        )
        assert denied.status_code == 410
        assert browser.get(f"{ANSWERS}/{first['attempt_id']}").status_code == 410

    # 到期拒絕不能靠事後退款掩蓋一次本可避免的模型預留或外送。
    assert len(provider_script.requests) == 1
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM model_requests").fetchone() == (2,)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)


# 多個工作階段由資料庫協調；重啟既不重送也不讓新請求覆蓋尚未到期的執行。
def test_live_attempt_survives_new_app_then_late_reply_cannot_overtake_retry(
    answer_ready: Settings,
    provider_script: ProviderScript,
) -> None:
    waiting, release = Event(), Event()

    # 第一個重試已寫完 HTTP，但回覆延至另一個應用接手之後。
    async def pending() -> httpx.Response:
        waiting.set()
        assert await anyio.to_thread.run_sync(release.wait, 8)
        return interaction("requires_action", CALL)

    provider_script.responses.extend(
        [model_output(None, []), pending, model_output(None, [])]
    )
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as owner,
        ThreadPoolExecutor(1) as pool,
    ):
        login(owner)
        original = ask(owner).json()
        first_retry = pool.submit(
            owner.post, ANSWERS, headers=CSRF, json=retry_form(original)
        )
        try:
            assert waiting.wait(5)
            with TestClient(create_app(answer_ready), base_url=ORIGIN) as another:
                login(another)
                follow_up = {
                    "conversation_id": original["conversation_id"],
                    "question": "另一題",
                }

                # 同帳號不同 Session 在遠端等待期間真的重疊；新應用沒有重送或清除執行。
                with answer_ready.connect() as conn:
                    row = conn.execute(
                        "SELECT id FROM answer_attempts WHERE status = 'running'"
                    ).fetchone()
                    assert row is not None
                    running_id = row[0]
                current = {**retry_form(original), "attempt_id": str(running_id)}
                for body in (follow_up, current):
                    busy = another.post(ANSWERS, headers=CSRF, json=body)
                    assert busy.status_code == 409
                    assert busy.json() == {
                        "detail": "這段對話正在處理問題，請稍後再試。"
                    }
                assert len(provider_script.requests) == 2
                with answer_ready.connect() as conn:
                    assert conn.execute(
                        "SELECT count(*) FROM questions"
                    ).fetchone() == (1,)
                    assert conn.execute(
                        "SELECT count(*) FROM answer_attempts"
                    ).fetchone() == (2,)
                    assert conn.execute(
                        "SELECT used FROM daily_model_quota"
                    ).fetchone() == (3,)
                    # 模擬停機後留下已過期限的資料，不需要等真實的六十秒。
                    conn.execute(
                        "UPDATE answer_attempts SET deadline_at = clock_timestamp() WHERE id = %s",
                        (running_id,),
                    )

                # 明確重試才接手，原執行的遲到工具回覆不得再次外呼或寫入結果。
                accepted = another.post(ANSWERS, headers=CSRF, json=current)
                assert accepted.status_code == 200
                result = accepted.json()
                assert result["question_id"] == original["question_id"]
                assert result["attempt_id"] != current["attempt_id"]
                assert result["status"] == "insufficient_evidence"
                release.set()
                assert first_retry.result(timeout=5).status_code == 404
                assert another.get(f"{ANSWERS}/{result['attempt_id']}").json() == result
        finally:
            release.set()

    assert len(provider_script.requests) == 3
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM questions").fetchone() == (1,)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (4,)
        assert conn.execute(
            "SELECT status FROM answer_attempts ORDER BY attempt_number"
        ).fetchall() == [
            ("insufficient_evidence",),
            ("stopped",),
            ("insufficient_evidence",),
        ]


@pytest.mark.parametrize(
    ("owner_form", "other_form"), [(USER_FORM, SUPPORT_FORM), (SUPPORT_FORM, USER_FORM)]
)
# 兩種角色都只能追問、重試與讀取自己的對話；錯誤識別不得建立新問題或預留。
def test_conversation_writes_reject_other_owners_and_forged_inputs(
    answer_ready: Settings,
    provider_script: ProviderScript,
    owner_form: dict[str, str],
    other_form: dict[str, str],
) -> None:
    provider_script.responses.extend([model_output(None, []), model_output(None, [])])
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as owner,
        TestClient(create_app(answer_ready), base_url=ORIGIN) as other,
    ):
        login(owner, owner_form)
        login(other, other_form)
        first, second = ask(owner).json(), ask(owner).json()
        retry = retry_form(first)
        follow_up = {"conversation_id": first["conversation_id"], "question": "再問"}

        # 不存在與他人的資料共用固定回覆；即使是客服也不能讀取私人聊天。
        for body in (follow_up, retry):
            denied = other.post(ANSWERS, headers=CSRF, json=body)
            missing = other.post(
                ANSWERS, headers=CSRF, json={**body, "conversation_id": str(uuid4())}
            )
            assert denied.status_code == missing.status_code == 404
            assert denied.json() == missing.json()
        assert other.get(f"{ANSWERS}/{first['attempt_id']}").status_code == 404
        assert (
            owner.post(
                ANSWERS,
                headers=CSRF,
                json={**retry, "attempt_id": second["attempt_id"]},
            ).status_code
            == 404
        )

        # 入口拒絕混合操作、偽造模型資料、缺少來源及所有識別欄位的非法型別。
        for invalid in (
            {**retry, "question": "偽造新題"},
            {**retry, "history": [{"answer": "冒用回答", "citations": [1]}]},
            {**follow_up, "search_results": [{"id": "假資料"}]},
            {**follow_up, "conversation_id": None},
            {**retry, "conversation_id": 7},
            {**retry, "question_id": "not-a-uuid"},
            {**retry, "attempt_id": None},
        ):
            assert owner.post(ANSWERS, headers=CSRF, json=invalid).status_code == 422
        assert owner.post(ANSWERS, json=retry).status_code == 403

    assert len(provider_script.requests) == 2
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM questions").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM answer_attempts").fetchone() == (2,)


# 第十一個新題不占配額；即使已到上限，重試第十題也沿用同一個題目。
def test_ten_questions_reject_another_but_allow_retry_and_new_conversation(
    answer_ready: Settings,
    provider_script: ProviderScript,
) -> None:
    provider_script.responses.extend(model_output(None, []) for _ in range(12))
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        first = ask(browser).json()
        body = {"conversation_id": first["conversation_id"], "question": "追問"}
        for _ in range(9):
            reply = browser.post(ANSWERS, headers=CSRF, json=body)
            assert (
                reply.status_code == 200
                and reply.json()["status"] == "insufficient_evidence"
            )
        tenth = reply.json()
        capped = browser.post(ANSWERS, headers=CSRF, json=body)
        assert capped.status_code == 409
        assert capped.json() == {"detail": "這段對話已達 10 題上限，請開始新的對話。"}
        assert len(provider_script.requests) == 10

        retry = browser.post(ANSWERS, headers=CSRF, json=retry_form(tenth))
        assert retry.status_code == 200
        assert retry.json()["question_id"] == tenth["question_id"]
        assert retry.json()["attempt_id"] != tenth["attempt_id"]
        assert browser.post(ANSWERS, headers=CSRF, json=body).status_code == 409
        assert ask(browser).json()["conversation_id"] != first["conversation_id"]

    assert len(provider_script.requests) == 12
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM questions").fetchone() == (11,)
        assert conn.execute("SELECT count(*) FROM answer_attempts").fetchone() == (12,)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (13,)


# 換 Key 後不能把上一個設定保存的成功步驟送到新專案；新對話仍可正常使用。
def test_rotating_key_cannot_forward_saved_history(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    mocked_provider: ProviderFixture,
    provider_script: ProviderScript,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    provider_script.responses.extend(
        [
            interaction("requires_action", THOUGHT, CALL),
            model_output("有依據。", [2]),
            model_output(None, []),
        ]
    )
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        first = ask(browser).json()
        assert first["status"] == "answered"
        follow_up = {"conversation_id": first["conversation_id"], "question": "再問"}

        # 只操作測試憑證並經既有 CLI 重新驗證；原有對話未重新綁定。
        set_key(answer_ready.key_file, "fixture-rotated-conversation-key")
        _, probes = mocked_provider
        probes.append(success)
        assert cli.main(["verify-key"]) == 0
        for body in (follow_up, retry_form(first)):
            rejected = browser.post(ANSWERS, headers=CSRF, json=body)
            assert rejected.status_code == 503
            assert rejected.json() == {"detail": "Gemini 設定已變更，請重新提出問題。"}
        assert len(provider_script.requests) == 2
        assert ask(browser).json()["status"] == "insufficient_evidence"

    request = json.loads(provider_script.requests[-1].split(b"\r\n\r\n", 1)[1])
    assert len(request["input"]) == 1 and "signature" not in str(request["input"])


# 舊成功回應在重跑初始化後仍可讀，但缺少原始步驟的前文不能靜默遺失。
def test_startup_retains_legacy_answer_and_quota_but_cannot_forward_it(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    provider_script.responses.extend(
        [
            interaction("requires_action", CALL),
            model_output("舊版答案。", [2]),
            model_output(None, []),
        ]
    )
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        cookie = login(browser)
        legacy = ask(browser).json()
        assert legacy["status"] == "answered"

    # 新欄位採用舊列的預設值；問題、回應及用量仍由真實 HTTP 及 SDK 建立。
    with answer_ready.connect() as conn:
        conn.execute("UPDATE conversations SET key_version = NULL")
        conn.execute(
            "UPDATE answer_attempts SET deadline_at = NULL, model_steps = NULL"
        )
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as restarted:
        headers = {**CSRF, **replay_cookie(cookie)}
        assert (
            restarted.get(f"{ANSWERS}/{legacy['attempt_id']}", headers=headers).json()
            == legacy
        )
        denied = restarted.post(
            ANSWERS,
            headers=headers,
            json={"conversation_id": legacy["conversation_id"], "question": "追問"},
        )
        assert denied.status_code == 409
        assert denied.json() == {"detail": "這段對話缺少可延續的前文，請開始新的對話。"}

    assert len(provider_script.requests) == 2
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM questions").fetchone() == (1,)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (3,)


@pytest.mark.parametrize(
    ("first", "last", "expected", "search_found"),
    [
        (
            interaction("requires_action", CALL),
            interaction("requires_action", {**CALL, "id": None}),
            "invalid_output",
            True,
        ),
        (
            interaction("requires_action", CALL),
            interaction(
                "completed",
                {"type": "model_output", "content": [{"type": "text", "text": None}]},
            ),
            "invalid_output",
            True,
        ),
        (model_output("沒有工具也聲稱有來源", [1]), None, "invalid_output", False),
        (model_output(None, []), None, "insufficient_evidence", False),
        (
            interaction("requires_action", {**CALL, "name": "create_ticket"}),
            None,
            "invalid_output",
            False,
        ),
        (
            interaction(
                "requires_action", {**CALL, "arguments": {"query": "配送", "limit": 50}}
            ),
            None,
            "invalid_output",
            False,
        ),
        (interaction("requires_action", CALL, CALL), None, "invalid_output", False),
        (
            interaction(
                "requires_action",
                CALL,
                {"type": "function_result", "call_id": "fake", "result": "fake"},
            ),
            None,
            "invalid_output",
            False,
        ),
        (interaction("completed", CALL), None, "invalid_output", False),
        (interaction("incomplete", THOUGHT, CALL), None, "invalid_output", False),
        (
            interaction("requires_action", {**CALL, "arguments": {"query": "無關"}}),
            model_output("聲稱有結果", [1]),
            "invalid_output",
            False,
        ),
        (
            interaction("requires_action", CALL),
            model_output("偽造引用", [3]),
            "invalid_output",
            True,
        ),
        (
            interaction("requires_action", CALL),
            model_output("重複引用", [2, 2]),
            "invalid_output",
            True,
        ),
        (
            interaction("requires_action", CALL),
            model_output(None, []),
            "insufficient_evidence",
            True,
        ),
        (
            interaction("requires_action", CALL),
            interaction("requires_action", CALL),
            "invalid_output",
            True,
        ),
    ],
)
# 錯誤工具、未完成與偽造引用不會被解讀成成功，也不消耗額外模型呼叫。
def test_answer_rejects_untrusted_model_output(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
    first: httpx.Response,
    last: httpx.Response | None,
    expected: str,
    search_found: bool,
) -> None:
    # 文件確實可搜尋，無效回覆仍不可把文件內容當成模型的證據。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    provider_script.responses.append(first)
    if last is not None:
        provider_script.responses.append(last)
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        response = ask(browser)
        assert response.status_code == 200
        assert response.json()["status"] == expected
        assert response.json()["answer"] is None and response.json()["citations"] == []
        assert response.json()["search_results"] == (
            ORIGINAL_RESULTS["results"] if search_found else []
        )
        assert len(provider_script.requests) == (1 if last is None else 2)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (429, "rate_limited"),
        (502, "unavailable"),
        (307, "unavailable"),
        (408, "timed_out"),
        ("timeout", "timed_out"),
        ("network", "unavailable"),
        (401, "invalid"),
    ],
)
# 第二次外呼失敗時保留已搜尋的來源；只有明確拒絕憑證才關閉業務。
def test_answer_preserves_search_after_provider_failure(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
    code: int | str,
    status: str,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0

    # 已寫完本文才發生錯誤，可觀察到已送出額度，亦不許重試或跟隨轉址。
    async def failure() -> httpx.Response:
        if code == "timeout":
            raise httpcore.ReadTimeout("fixture read timeout")
        if code == "network":
            raise httpcore.ReadError("fixture broken connection")
        assert isinstance(code, int)
        return httpx.Response(
            code,
            json={"error": {"message": FIXTURE_KEY}},
            headers={"location": "https://invalid.example"},
        )

    provider_script.responses.extend([interaction("requires_action", CALL), failure])
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        response = ask(browser)
        if status == "invalid":
            assert response.status_code == 503
            assert "search_results" not in response.json()
        else:
            assert response.status_code == 200
            assert response.json()["status"] == status
            assert (
                response.json()["answer"] is None and response.json()["citations"] == []
            )
            assert response.json()["search_results"] == ORIGINAL_RESULTS["results"]
        assert FIXTURE_KEY not in response.text
        assert browser.get("/api/setup").json()["status"] == (
            "invalid" if code == 401 else "ready"
        )

    # 已確定寫完兩次模型請求，啟用請求另外計入部署用量。
    assert len(provider_script.requests) == 2
    with answer_ready.connect() as conn:
        assert conn.execute(
            "SELECT delivery FROM model_requests WHERE username = 'user'"
        ).fetchall() == [("sent",), ("sent",)]
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (3,)


# 用兩個工具和三次模型請求走到上限；第三次仍要求工具時不可再執行或外呼。
def test_answer_enforces_both_model_and_tool_budgets(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    # 第三筆若誤執行會取得不同來源，可從結果辨別是否真的遵守工具上限。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    provider_script.responses.extend(
        [
            interaction("requires_action", CALL),
            interaction(
                "requires_action",
                {**CALL, "id": "next", "arguments": {"query": "找不到"}},
            ),
            interaction(
                "requires_action",
                {**CALL, "id": "excess", "arguments": {"query": "退貨"}},
            ),
        ]
    )
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        response = ask(browser)
        assert response.status_code == 200
        assert response.json()["status"] == "invalid_output"
        assert response.json()["search_results"] == ORIGINAL_RESULTS["results"]
    assert len(provider_script.requests) == 3


# 從同源驗證到 Unicode 上限與擁有權；不合法的請求不得建立對話或外呼。
def test_answer_input_and_private_readback(
    answer_ready: Settings,
    provider_script: ProviderScript,
) -> None:
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        # 未登入或 CSRF 缺失時拒絕；原始 bytes 保留重複欄位及確切碼位。
        assert ask(browser).status_code == 401
        owner = login(browser)
        assert browser.post(ANSWERS, json={"question": QUESTION}).status_code == 403
        for raw in (
            b'{"question":"first","question":"last"}',
            b'{"question":"ok","history":[]}',
            b'{"question":5}',
            b'{"question":" "}',
            b'{"question":"\\ud800"}',
            b'{"question":',
            json.dumps({"question": "🧪" * 2001}, ensure_ascii=False).encode("utf-8"),
        ):
            response = browser.post(
                ANSWERS,
                headers={**CSRF, "Content-Type": "application/json"},
                content=raw,
            )
            assert response.status_code == 422
            assert response.json() == {"detail": "問題欄位格式不正確。"}
        with answer_ready.connect() as conn:
            assert conn.execute("SELECT count(*) FROM conversations").fetchone() == (0,)

        # 兩千個非 BMP 字元可以送出；客服角色也沒有讀取私人問答的特權。
        provider_script.responses.append(model_output(None, []))
        response = ask(browser, "🧪" * 2000)
        assert response.status_code == 200
        path = f"{ANSWERS}/{response.json()['attempt_id']}"
        assert browser.get(path).status_code == 200
        with TestClient(create_app(answer_ready), base_url=ORIGIN) as support:
            login(support, SUPPORT_FORM)
            assert support.get(path).status_code == 404
            assert support.get(f"{ANSWERS}/not-a-uuid").status_code == 404
        assert browser.post("/api/logout", headers=CSRF).status_code == 204
        assert browser.get(path, headers=replay_cookie(owner)).status_code == 401
    assert len(provider_script.requests) == 1


@pytest.mark.parametrize("logout", [True, False])
# 在等待遠端時登出或令 Session 恰好到期，已寫入的請求保留額度，但不查資料或續呼。
def test_answer_does_not_continue_when_login_expires_during_model_wait(
    answer_ready: Settings,
    provider_script: ProviderScript,
    clock: list[datetime],
    logout: bool,
) -> None:
    waiting, release = Event(), Event()

    # 真正開始讀遠端回應後，先讓測試撤銷或使登入恰好到期。
    async def pending() -> httpx.Response:
        waiting.set()
        assert await anyio.to_thread.run_sync(release.wait, 5)
        return interaction("requires_action", CALL)

    provider_script.responses.append(pending)
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        ThreadPoolExecutor(2) as pool,
    ):
        login(browser)
        answer = pool.submit(ask, browser)
        assert waiting.wait(5)
        try:
            if logout:
                removed = pool.submit(browser.post, "/api/logout", headers=CSRF)
                assert removed.result(timeout=2).status_code == 204
            else:
                clock[0] += timedelta(minutes=30)
        finally:
            # 即使驗證登出失敗，也釋放模擬供應商避免留下執行緒。
            release.set()
        rejected = answer.result(timeout=5)
        assert rejected.status_code == 401
        assert rejected.json() == {"detail": "請先登入。"}

    # 已送出且只有一筆；暫存只留下停止狀態，沒有模型文字或來源資料。
    assert len(provider_script.requests) == 1
    with answer_ready.connect() as conn:
        assert conn.execute(
            "SELECT delivery FROM model_requests WHERE username = 'user'"
        ).fetchall() == [("sent",)]
        assert conn.execute(
            "SELECT status, answer, search_results FROM answer_attempts"
        ).fetchone() == ("stopped", None, [])


@pytest.mark.parametrize("lose_activation", [True, False])
# 真正卡在最後授權鎖時改變啟用或期限，確認沒有任何 HTTP 位元組送出。
def test_answer_rechecks_session_and_activation_after_waiting_for_dispatch(
    answer_ready: Settings,
    provider_script: ProviderScript,
    clock: list[datetime],
    lose_activation: bool,
) -> None:
    connecting, release = Event(), Event()

    # 模型連線先卡住，讓測試可在初始問答交易提交後持有最後一把鎖。
    async def connect() -> None:
        connecting.set()
        assert await anyio.to_thread.run_sync(release.wait, 5)

    provider_script.before_connect = connect
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        ThreadPoolExecutor(1) as pool,
    ):
        login(browser)
        future = pool.submit(ask, browser)
        assert connecting.wait(5)
        with answer_ready.connect() as blocker:
            blocker.execute("SELECT singleton FROM activation FOR UPDATE")
            release.set()
            wait_for_search_lock(answer_ready, blocker.info.backend_pid)
            if lose_activation:
                blocker.execute("UPDATE activation SET status = 'invalid'")
            else:
                clock[0] += timedelta(minutes=30)
        response = future.result(timeout=5)
        assert response.status_code == (503 if lose_activation else 401)

    # 額度已預留但寫入前被拒絕，留本機取消紀錄並只退這一次。
    assert provider_script.requests == []
    with answer_ready.connect() as conn:
        assert conn.execute(
            "SELECT delivery FROM model_requests WHERE username = 'user'"
        ).fetchall() == [("unsent",)]
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (1,)


# 以短期限取消已送出的第二次外呼，晚到的模型結果不會改寫逾時終態。
def test_answer_total_timeout_keeps_only_real_evidence(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    monkeypatch.setattr(answers, "ATTEMPT_SECONDS", 1.5)

    # 等待中的供應商回覆不會因測試提早結束而自行產生一個成功結果。
    async def pending() -> httpx.Response:
        await anyio.sleep_forever()
        return model_output("一般配送需兩個工作天。", [2])

    provider_script.responses.extend([interaction("requires_action", CALL), pending])
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        response = ask(browser)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "timed_out"
        assert payload["answer"] is None and payload["citations"] == []
        assert payload["search_results"] == ORIGINAL_RESULTS["results"]
        assert browser.get(f"{ANSWERS}/{payload['attempt_id']}").json() == payload
    assert len(provider_script.requests) == 2


# 期限內完成的工具證據不因執行緒晚交回而遺失，也不能再發一次模型請求。
def test_answer_retains_timely_search_when_worker_returns_after_deadline(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    monkeypatch.setattr(answers, "ATTEMPT_SECONDS", 2.0)
    loop: asyncio.AbstractEventLoop | None = None

    # 使用真實模型傳輸替身收到工具要求，同時取得執行本次 HTTP 的事件迴圈。
    async def first_response() -> httpx.Response:
        nonlocal loop
        loop = asyncio.get_running_loop()
        return interaction("requires_action", CALL)

    provider_script.responses.append(first_response)

    # 先完整執行真實工具交易，再由同一事件迴圈於既有期限之後放行結果。
    def return_late(
        settings: Settings,
        digest: bytes,
        attempt: Attempt,
        query: str,
        deadline: float,
    ) -> list[knowledge.SearchResult]:
        results = search_attempt(settings, digest, attempt, query, deadline)
        assert [source.model_dump() for source in results] == ORIGINAL_RESULTS[
            "results"
        ]
        assert monotonic() < deadline
        assert loop is not None
        released = Event()
        loop.call_soon_threadsafe(
            loop.call_later, deadline - monotonic() + 0.01, released.set
        )
        assert released.wait(5)
        assert monotonic() >= deadline
        return results

    monkeypatch.setattr(answers, "search_attempt", return_late)
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        response = ask(browser)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "timed_out"
        assert payload["answer"] is None and payload["citations"] == []
        assert payload["search_results"] == ORIGINAL_RESULTS["results"]
        assert browser.get(f"{ANSWERS}/{payload['attempt_id']}").json() == payload
    assert len(provider_script.requests) == 1


@pytest.mark.parametrize("blocked_table", ["conversations", "answer_attempts"])
# 等候對話鎖或最終更新的列鎖超過總期限，均不能保存或交付成功。
def test_answer_deadline_is_checked_after_final_database_lock(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
    blocked_table: str,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    waiting, release = Event(), Event()
    final_clock = [monotonic()]

    # 只控制終態寫入處的時間，實際模型工作及鎖等待保持真實。
    def final_time() -> float:
        return final_clock[0]

    monkeypatch.setattr("backend.answer_storage.monotonic", final_time)

    # 第二次模型回覆要等到測試持有對話鎖才送回。
    async def pending() -> httpx.Response:
        waiting.set()
        assert await anyio.to_thread.run_sync(release.wait, 5)
        return model_output("一般配送需兩個工作天。", [2])

    provider_script.responses.extend([interaction("requires_action", CALL), pending])
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        ThreadPoolExecutor(1) as pool,
    ):
        login(browser)
        future = pool.submit(ask, browser)
        assert waiting.wait(5)
        with answer_ready.connect() as blocker:
            if blocked_table == "conversations":
                blocker.execute("SELECT id FROM conversations FOR UPDATE")
            else:
                blocker.execute("SELECT id FROM answer_attempts FOR UPDATE")
            release.set()
            wait_for_search_lock(answer_ready, blocker.info.backend_pid)
            final_clock[0] = float("inf")
        response = future.result(timeout=5)
        assert response.status_code == 200
        assert response.json()["status"] == "timed_out"
        assert response.json()["answer"] is None and response.json()["citations"] == []
        assert response.json()["search_results"] == ORIGINAL_RESULTS["results"]
        assert (
            browser.get(f"{ANSWERS}/{response.json()['attempt_id']}").json()
            == response.json()
        )


# 模型已收到第一次請求後透過實際 ASGI 通知斷線，工作必須取消並保存送出紀錄。
def test_answer_disconnect_cancels_provider_and_stops_attempt(
    answer_ready: Settings,
    provider_script: ProviderScript,
) -> None:
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        cookie = login(browser)
        events: list[Message] = []

        # 不讓測試 HTTP 客戶端吃掉斷線；直接從正式應用程式的 ASGI 入口送事件。
        async def disconnect() -> None:
            ready, cancelled = asyncio.Event(), asyncio.Event()
            delivered = False

            # 發送完成後在遠端等回應，觀察取消是否真的傳到網路邊界。
            async def pending() -> httpx.Response:
                ready.set()
                try:
                    await anyio.sleep_forever()
                finally:
                    cancelled.set()
                return interaction("requires_action", CALL)

            provider_script.responses.append(pending)
            scope: Scope = {
                "type": "http",
                "method": "POST",
                "path": ANSWERS,
                "raw_path": ANSWERS.encode("ascii"),
                "root_path": "",
                "query_string": b"",
                "scheme": "http",
                "http_version": "1.1",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "client": ("192.0.2.1", 50000),
                "server": ("localhost", 8020),
                "headers": [
                    (b"host", b"localhost:8020"),
                    (b"content-type", b"application/json"),
                ]
                + [
                    (name.lower().encode("ascii"), value.encode("ascii"))
                    for name, value in {**CSRF, **replay_cookie(cookie)}.items()
                ],
            }

            # 先送完整合法請求；當真正進入供應商讀取階段才通知斷線。
            async def receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": json.dumps({"question": QUESTION}).encode("utf-8"),
                    }
                await ready.wait()
                return {"type": "http.disconnect"}

            # 收集實際 ASGI 輸出，核對斷線後沒有私人內容。
            async def send(message: Message) -> None:
                events.append(message)

            with anyio.fail_after(5):
                await browser.app(scope, receive, send)
            assert cancelled.is_set()

        asyncio.run(disconnect())
        assert len(provider_script.requests) == 1
        assert not any(message.get("body") for message in events)
    with answer_ready.connect() as conn:
        assert conn.execute(
            "SELECT status, answer FROM answer_attempts"
        ).fetchone() == ("stopped", None)
        assert conn.execute(
            "SELECT delivery FROM model_requests WHERE username = 'user'"
        ).fetchone() == ("sent",)


# 部分寫入仍視為送達不明；連線尚未開始寫入時則不消耗模型請求。
def test_answer_partial_write_keeps_unknown_reservation(
    answer_ready: Settings,
    provider_script: ProviderScript,
) -> None:
    # 直接在實際 httpcore 寫入路徑觸發寫入逾時，尚無完整本文或 HTTP 回覆。
    async def write_timeout() -> None:
        raise httpcore.WriteTimeout("fixture write timeout")

    provider_script.before_write = write_timeout
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        response = ask(browser)
        assert response.status_code == 200
        assert response.json()["status"] == "timed_out"
    with answer_ready.connect() as conn:
        assert conn.execute(
            "SELECT delivery FROM model_requests WHERE username = 'user'"
        ).fetchone() == ("unknown",)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)


# 同一帳號同時搶最後一筆用量，跨重啟仍受限，另一帳號仍有獨立額度。
def test_answer_account_quota_is_atomic_and_persistent(
    answer_ready: Settings,
    provider_script: ProviderScript,
) -> None:
    barrier = Barrier(2, timeout=5)
    provider_script.responses.extend([model_output(None, []), model_output(None, [])])
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        cookie = login(browser)

        # 用實際 PostgreSQL 預留入口建立接近個人上限的初始狀態。
        with answer_ready.connect() as conn:
            for _ in range(19):
                assert reserve_request(conn, uuid4(), username="user")

        # 兩個 HTTP 請求使用同一帳號，啟動順序不影響原子上限。
        def compete() -> str:
            barrier.wait()
            result = ask(browser)
            assert result.status_code == 200
            return cast(str, result.json()["status"])

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(compete)
            second = pool.submit(compete)
            assert sorted((first.result(timeout=8), second.result(timeout=8))) == [
                "insufficient_evidence",
                "rate_limited",
            ]
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as restarted:
        denied = restarted.post(
            ANSWERS,
            headers={**CSRF, **replay_cookie(cookie)},
            json={"question": QUESTION},
        )
        assert denied.json()["status"] == "rate_limited"
        login(restarted, SUPPORT_FORM)
        assert ask(restarted).json()["status"] == "insufficient_evidence"
    assert len(provider_script.requests) == 2
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (22,)


# 子程序執行真實 CLI；只沿用已通過識別檢查的測試庫及受控外部回覆。
def verify_in_process(settings: Settings, result_pipe: ProcessPipe) -> None:
    assert settings.database_dsn == TEST_DSN
    sent = 0

    # 子程序沒有繼承 pytest 的環境替換；明確使用同一個隔離設定。
    def configured() -> Settings:
        return settings

    # 留住實際 CLI、SDK 和寫入檢查，僅接管網路，不接觸外部服務。
    def send(_: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal sent
        assert request.url.host == "generativelanguage.googleapis.com"
        trace = cast(
            Callable[[str, dict[str, object]], None], request.extensions["trace"]
        )
        trace("http11.send_request_headers.started", {})
        sent += 1
        return success()

    with pytest.MonkeyPatch.context() as patch, result_pipe:
        patch.setattr(Settings, "from_environment", staticmethod(configured))
        patch.setattr(httpx.HTTPTransport, "handle_request", send)
        result_pipe.send((cli.main(["verify-key"]), sent))


# 真正的 CLI 子程序和 HTTP 服務各自在 PostgreSQL 等最後一筆用量；重啟沿用同一份額度。
def test_cli_and_answer_share_last_service_quota(
    answer_ready: Settings,
    provider_script: ProviderScript,
) -> None:
    connecting, release = Event(), Event()
    provider_script.responses.append(model_output(None, []))

    # 問答先完成初始 Session 檢查，讓兩個程序都能實際等在額度鎖。
    async def connect() -> None:
        connecting.set()
        assert await anyio.to_thread.run_sync(release.wait, 8)

    provider_script.before_connect = connect
    context = multiprocessing.get_context("spawn")

    @contextmanager
    # 啟動新程序，並在成功或失敗後回收，避免任何背景工作留在隔離環境。
    def cli_worker() -> Iterator[ProcessPipe]:
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=verify_in_process, args=(answer_ready, sender))
        with receiver, sender:
            process.start()
            sender.close()
            try:
                yield receiver
            finally:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                assert process.exitcode == 0

    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        ThreadPoolExecutor(1) as pool,
    ):
        cookie = login(browser)
        with answer_ready.connect() as conn:
            conn.execute("UPDATE daily_model_quota SET used = 99")

        # HTTP 等到連線階段才啟動 CLI，避開 CLI 初始化鎖與 Session 檢查的假競爭。
        pending = pool.submit(ask, browser)
        assert connecting.wait(5)
        with answer_ready.connect() as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(7680281636712970524)")
            with cli_worker() as cli_reply:
                try:
                    wait_for_search_lock(answer_ready, blocker.info.backend_pid)
                    release.set()

                    # 兩條實際資料庫連線都必須已等待同一個額度 advisory lock。
                    deadline = monotonic() + 5
                    with answer_ready.connect() as observer:
                        observer.autocommit = True
                        while observer.execute(
                            """
                            SELECT count(*) FROM pg_stat_activity
                            WHERE datname = current_database()
                              AND wait_event_type = 'Lock' AND wait_event = 'advisory'
                              AND query = 'SELECT pg_advisory_xact_lock(7680281636712970524)'
                            """
                        ).fetchone() != (2,):
                            assert monotonic() < deadline
                finally:
                    # 成功或失敗都釋放鎖與傳輸，讓子程序可正常退出。
                    release.set()
                    blocker.commit()

                # 兩個程序搶剩餘用量，只有一邊可實際送出 HTTP。
                response = pending.result(timeout=8)
                assert response.status_code == 200
                assert cli_reply.poll(8)
                cli_status, cli_sent = cli_reply.recv()
                assert (cli_status, response.json()["status"]) in {
                    (0, "rate_limited"),
                    (1, "insufficient_evidence"),
                }
                assert len(provider_script.requests) + cli_sent == 1

    # 啟動新的 CLI 程序與 HTTP 應用，兩邊都不能重設或再次消耗已用完的額度。
    with cli_worker() as restarted_reply:
        assert restarted_reply.poll(8)
        assert restarted_reply.recv() == (1, 0)
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as restarted:
        denied = restarted.post(
            ANSWERS,
            headers={**CSRF, **replay_cookie(cookie)},
            json={"question": QUESTION},
        )
        assert denied.status_code == 200 and denied.json()["status"] == "rate_limited"
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (100,)


@pytest.mark.parametrize("maximum", [False, True])
# 私人 GET 在到期前可讀、等號即拒絕；重啟只清掉暫存，不重跑模型或刪除用量。
def test_answer_expiry_and_startup_cleanup(
    answer_ready: Settings,
    provider_script: ProviderScript,
    clock: list[datetime],
    maximum: bool,
) -> None:
    provider_script.responses.append(model_output(None, []))
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        payload = ask(browser).json()
        path = f"{ANSWERS}/{payload['attempt_id']}"

        # 絕對期限使用既有長對話的時間形狀，保持 Session 和閒置期限仍有效。
        if maximum:
            with answer_ready.connect() as conn:
                conn.execute(
                    "UPDATE conversations SET created_at = %s",
                    (clock[0] - timedelta(minutes=119),),
                )
        clock[0] += timedelta(minutes=1 if maximum else 30) - timedelta(microseconds=1)
        if not maximum:
            # 同步刷新登入，以區分登入期限與獨立的對話暫存期限。
            assert browser.get("/api/session", headers=CSRF).status_code == 200

        # 讀取不刷新暫存，時間剛好等於期限時拒絕。
        assert browser.get(path).status_code == 200
        clock[0] += timedelta(microseconds=1)
        assert browser.get(path).status_code == 410
        # 過期的自有對話不能追問或重試，即使登入尚未到期。
        for body in (
            {"question": QUESTION, "conversation_id": payload["conversation_id"]},
            retry_form(payload),
        ):
            denied = browser.post(ANSWERS, headers=CSRF, json=body)
            assert denied.status_code == 410
            assert denied.json() == {"detail": "問答暫存已到期。"}
        cookie = browser.cookies[sessions.COOKIE_NAME]
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as restarted:
        assert restarted.get(path, headers=replay_cookie(cookie)).status_code == 404
    with answer_ready.connect() as conn:
        for table in ("conversations", "questions", "answer_attempts"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)
    assert len(provider_script.requests) == 1


# 服務運行期間依相同期限清理，清理後仍可讀取與使用當日用量。
def test_answer_periodic_cleanup(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
    clock: list[datetime],
) -> None:
    monkeypatch.setattr("backend.app.PURGE_INTERVAL_SECONDS", 0.05)
    provider_script.responses.append(model_output(None, []))
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        assert ask(browser).status_code == 200
        clock[0] += timedelta(minutes=30)

        # 透過獨立連線觀察週期清理提交，不以手動清理替代實際服務行為。
        limit = monotonic() + 5
        with answer_ready.connect() as conn:
            conn.autocommit = True
            while conn.execute("SELECT count(*) FROM answer_attempts").fetchone() != (
                0,
            ):
                assert monotonic() < limit
            assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)


# 等候工具授權鎖後若已到總期限，連實際知識 SELECT 都不能啟動。
def test_answer_does_not_start_knowledge_query_after_tool_lock_deadline(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    waiting, release = Event(), Event()
    tool_clock = [monotonic()]
    queries: list[str] = []

    # 控制同步交易中的判斷時間；以呼叫真實查詢的記錄證明是否開始讀資料。
    def transaction_time() -> float:
        return tool_clock[0]

    original = knowledge.find_fragments

    # 保留真實 SQL 與來源，以便測出即使結果遭捨棄也已發動的越期查詢。
    def observe(
        conn: Connection[tuple[object, ...]], query: str
    ) -> list[knowledge.SearchResult]:
        queries.append(query)
        return original(conn, query)

    monkeypatch.setattr("backend.answer_storage.monotonic", transaction_time)
    monkeypatch.setattr(knowledge, "find_fragments", observe)

    # 等第一個模型請求完全寫入，再由測試取得工具授權會等待的啟用列鎖。
    async def pending() -> httpx.Response:
        waiting.set()
        assert await anyio.to_thread.run_sync(release.wait, 5)
        return interaction("requires_action", CALL)

    provider_script.responses.append(pending)
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        ThreadPoolExecutor(1) as pool,
    ):
        login(browser)
        future = pool.submit(ask, browser)
        assert waiting.wait(5)
        with answer_ready.connect() as blocker:
            blocker.execute("SELECT singleton FROM activation FOR UPDATE")
            release.set()
            wait_for_search_lock(answer_ready, blocker.info.backend_pid)
            tool_clock[0] = float("inf")
        response = future.result(timeout=5)
        assert response.status_code == 200
        assert response.json()["status"] == "timed_out"
        assert response.json()["search_results"] == []
        assert queries == []
    assert len(provider_script.requests) == 1


# 第一個工具已成功時，第二次預留若卡住，不可捨棄已取得且仍有權讀取的證據。
def test_answer_preserves_evidence_after_later_quota_lock_timeout(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    connecting, release = Event(), Event()

    # 第二次 TLS 建立前讓測試持有額度鎖，第一個模型與工具已經完成。
    async def connect() -> None:
        if provider_script.requests:
            connecting.set()
            assert await anyio.to_thread.run_sync(release.wait, 5)

    provider_script.before_connect = connect
    provider_script.responses.append(interaction("requires_action", CALL))
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        ThreadPoolExecutor(1) as pool,
    ):
        login(browser)
        future = pool.submit(ask, browser)
        assert connecting.wait(5)
        with answer_ready.connect() as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(7680281636712970524)")
            release.set()
            wait_for_search_lock(answer_ready, blocker.info.backend_pid)
            response = future.result(timeout=8)
        assert response.status_code == 200
        assert response.json()["status"] == "unavailable"
        assert response.json()["answer"] is None
        assert response.json()["search_results"] == ORIGINAL_RESULTS["results"]
        assert (
            browser.get(f"{ANSWERS}/{response.json()['attempt_id']}").json()
            == response.json()
        )
    assert len(provider_script.requests) == 1
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)


# 拒絕必須在啟用列仍上鎖時生效，且已在等待的業務要在取得鎖後看到最新拒絕。
def test_credential_rejection_settles_before_waiting_for_activation_lock(
    answer_ready: Settings,
    provider_script: ProviderScript,
    mocked_provider: ProviderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    waiting, release = Event(), Event()

    # 收到明確拒絕前，讓測試先持有啟用列鎖，模型連線本身沒有保留它。
    async def reject() -> httpx.Response:
        waiting.set()
        assert await anyio.to_thread.run_sync(release.wait, 5)
        return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})

    # 用另一個帳號通過 HTTP 啟用閘門，確定它等的是啟用列，不是原問答的 Session。
    def search(browser: TestClient) -> httpx.Response:
        return cast(
            httpx.Response,
            browser.post("/api/knowledge/search", headers=CSRF, json={"query": "配送"}),
        )

    provider_script.responses.append(reject)
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        TestClient(create_app(answer_ready), base_url=ORIGIN) as support,
        ThreadPoolExecutor(2) as pool,
    ):
        login(browser)
        login(support, SUPPORT_FORM)
        future = pool.submit(ask, browser)
        assert waiting.wait(5)
        with answer_ready.connect() as blocker:
            blocker.execute("SELECT singleton FROM activation FOR UPDATE")
            pending_search = pool.submit(search, support)
            try:
                wait_for_search_lock(answer_ready, blocker.info.backend_pid)
            finally:
                # 若觀察鎖等待失敗，也要讓已送出的模型請求結束。
                release.set()

            # 已送出入帳與拒絕必須一併提交；舊啟用列不需改寫或釋放。
            with answer_ready.connect() as observer:
                observer.autocommit = True
                limit = monotonic() + 5
                while observer.execute(
                    "SELECT delivery FROM model_requests WHERE username = 'user'"
                ).fetchone() != ("sent",):
                    assert monotonic() < limit
            assert browser.get("/api/setup").json()["status"] == "invalid"
            assert ask(browser).status_code == 503

        # 已通過入口檢查的搜尋也要在拿到鎖後拒絕，不提供任何私人或知識結果。
        assert pending_search.result(timeout=5).status_code == 503
        rejected = future.result(timeout=5)
        assert rejected.status_code == 503 and "search_results" not in rejected.json()
    assert len(provider_script.requests) == 1

    # 新應用程序讀同一份帳本；當日配額及原始 CLI 請求仍完整。
    with answer_ready.connect() as conn:
        ledger = conn.execute(
            "SELECT id, service_day, username, delivery FROM model_requests ORDER BY id"
        ).fetchall()
        assert len(ledger) == 2
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)

    # 只有新一輪明確成功才能覆蓋相同 Key 的舊拒絕；暫時限流不會重開業務。
    def limited() -> httpx.Response:
        return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})

    with TestClient(create_app(answer_ready), base_url=ORIGIN) as restarted:
        assert restarted.get("/api/setup").json()["status"] == "invalid"
        assert ask(restarted).status_code == 503
        _, responses = mocked_provider
        responses.extend((limited, success))
        assert cli.main(["verify-key"]) == 1
        assert restarted.get("/api/setup").json()["status"] == "invalid"
        assert cli.main(["verify-key"]) == 0
        assert restarted.get("/api/setup").json()["status"] == "ready"
        login(restarted)
        assert search(restarted).json() == ORIGINAL_RESULTS

    # 重新驗證只新增使用量，原先兩筆送出紀錄和已計入的配額沒有改寫。
    with answer_ready.connect() as conn:
        assert (
            conn.execute(
                "SELECT id, service_day, username, delivery FROM model_requests WHERE id = ANY(%s) ORDER BY id",
                ([row[0] for row in ledger],),
            ).fetchall()
            == ledger
        )
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (4,)


# 舊回覆即使晚於更新成功抵達，舊驗證序號仍不會使目前的 Key 失效。
def test_late_business_rejection_cannot_poison_newer_success(
    answer_ready: Settings,
    provider_script: ProviderScript,
    mocked_provider: ProviderFixture,
) -> None:
    waiting, release = Event(), Event()

    # 讓已使用舊驗證序號送出的模型請求等到新探測成功才回覆。
    async def reject() -> httpx.Response:
        waiting.set()
        assert await anyio.to_thread.run_sync(release.wait, 5)
        return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})

    provider_script.responses.append(reject)
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        ThreadPoolExecutor(1) as pool,
    ):
        login(browser)
        future = pool.submit(ask, browser)
        try:
            assert waiting.wait(5)
            _, responses = mocked_provider
            responses.append(success)
            assert cli.main(["verify-key"]) == 0
        finally:
            # 無論新探測是否成功，都要讓已送出的業務請求結束。
            release.set()
        response = future.result(timeout=5)
        assert (
            response.status_code == 200 and response.json()["status"] == "unavailable"
        )
        assert browser.get("/api/setup").json()["status"] == "ready"
    assert len(provider_script.requests) == 1


# 即使還能發第三次模型請求，已執行兩個工具時也不可再執行第三個。
def test_answer_tool_limit_is_independent_of_model_request_limit(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    provider_script.responses.extend(
        [
            interaction(
                "requires_action",
                CALL,
                {**CALL, "id": "another", "arguments": {"query": "找不到"}},
            ),
            interaction(
                "requires_action",
                {**CALL, "id": "excess", "arguments": {"query": "退貨"}},
            ),
        ]
    )
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        response = ask(browser)
        assert response.status_code == 200
        assert response.json()["status"] == "invalid_output"
        assert response.json()["search_results"] == ORIGINAL_RESULTS["results"]
    assert len(provider_script.requests) == 2


@pytest.mark.parametrize("change", ["day", "key"])
# 在最後授權取得後、實際寫入 HTTP 前變更日期或 Key；舊預留只能退回。
def test_answer_rechecks_key_and_day_at_wire_admission(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
    change: str,
) -> None:
    with answer_ready.connect() as conn:
        row = conn.execute("SELECT service_day FROM daily_model_quota").fetchone()
        assert row is not None
        original_day = cast(date, row[0])
    current_day = [original_day]
    original = lock_dispatch

    # 首次預留仍使用 PostgreSQL 真實日期，最後檢查才會看到日界變化。
    def wire_day() -> date:
        return current_day[0]

    # 真正取得 Session/啟用列鎖後，在最後非同步等待前替換檔案或跨日。
    def after_lock(
        settings: Settings, digest: bytes, attempt: Attempt, *args: float
    ) -> Dispatch:
        result = original(settings, digest, attempt, *args)
        if change == "key":
            set_key(settings.key_file, "fixture-rotated-before-wire")
        else:
            current_day[0] += timedelta(days=1)
        return result

    monkeypatch.setattr(answers, "service_day_now", wire_day, raising=False)
    monkeypatch.setattr(answers, "lock_dispatch", after_lock)
    provider_script.responses.append(model_output(None, []))
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        response = ask(browser)
        if change == "key":
            assert response.status_code == 503
            assert browser.get("/api/setup").json()["status"] == "unverified"
        else:
            assert response.status_code == 200
            assert response.json()["status"] == "rate_limited"
            assert browser.get("/api/setup").json()["status"] == "ready"

    # 明確未送出的舊日期請求只退原預留，完全沒有任何業務 HTTP 寫入。
    assert provider_script.requests == []
    with answer_ready.connect() as conn:
        assert conn.execute(
            "SELECT service_day, delivery FROM model_requests WHERE username = 'user'"
        ).fetchone() == (original_day, "unsent")
        assert conn.execute(
            "SELECT service_day, used FROM daily_model_quota"
        ).fetchall() == [(original_day, 1)]


# 將工作階段放在絕對期限前一微秒，保留正常閒置刷新行為。
def set_session_near_absolute_expiry(settings: Settings, at: datetime) -> None:
    with settings.connect() as conn:
        conn.execute(
            "UPDATE login_sessions SET created_at = %s",
            (at - sessions.SESSION_MAX + timedelta(microseconds=1),),
        )


# 送出授權交回事件迴圈時登入若到期，HTTP 標頭也不能寫入，且須退回預留。
def test_absolute_session_expiry_between_admission_and_wire(
    answer_ready: Settings,
    provider_script: ProviderScript,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_script.responses.append(model_output(None, []))
    writes: list[bool] = []

    # 保留實際的鎖及 SDK 傳輸，僅在它們交接時推進測試時鐘。
    def after_lock(
        settings: Settings, digest: bytes, attempt: Attempt, deadline: float
    ) -> Dispatch:
        result = lock_dispatch(settings, digest, attempt, deadline)
        clock[0] += timedelta(microseconds=1)
        return result

    # 直接記錄任何 HTTP 串流寫入，連只有標頭的部分傳輸也能察覺。
    async def record_write() -> None:
        writes.append(True)

    provider_script.before_write = record_write
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        set_session_near_absolute_expiry(answer_ready, clock[0])
        monkeypatch.setattr(answers, "lock_dispatch", after_lock)
        assert ask(browser).status_code == 401

    # 到期撤銷必須提交，原本已完成的 Key 驗證用量仍留在帳本。
    assert writes == [] and provider_script.requests == []
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM login_sessions").fetchone() == (0,)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (1,)
        assert conn.execute(
            "SELECT delivery FROM model_requests WHERE username = 'user'"
        ).fetchone() == ("unsent",)


# 工作階段若在等對話列鎖時到期，拒絕之前也不能讀取私人回答欄位。
def test_get_expiry_after_conversation_lock_does_not_read_payload(
    answer_ready: Settings,
    provider_script: ProviderScript,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_script.responses.append(model_output(None, []))
    actual = Connection.execute
    payload_sql = "SELECT q.conversation_id, q.id, a.status, a.answer, a.citations"
    private_reads: list[bool] = []

    # 只記錄有沒有送出查詢，仍由 PostgreSQL 執行所有實際 SQL。
    def observe_execute(
        self: Connection[tuple[object, ...]],
        query: str | bytes | SQL | Composed,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool = False,
    ) -> Cursor[tuple[object, ...]]:
        if payload_sql in str(query):
            private_reads.append(True)
        return actual(self, query, params, prepare=prepare, binary=binary)

    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        ThreadPoolExecutor(1) as pool,
    ):
        # 先用公開 API 建立回答，確認到期前仍能讀回相同結果。
        login(browser)
        prior = ask(browser)
        assert prior.status_code == 200
        path = f"{ANSWERS}/{prior.json()['attempt_id']}"
        set_session_near_absolute_expiry(answer_ready, clock[0])
        assert browser.get(path).json() == prior.json()
        monkeypatch.setattr(Connection, "execute", observe_execute)

        # 等 GET 真的被對話列鎖阻擋才跨過絕對期限，再讓查詢繼續。
        with answer_ready.connect() as blocker:
            blocker.execute("SELECT id FROM conversations FOR UPDATE")
            pending = pool.submit(browser.get, path)
            wait_for_search_lock(answer_ready, blocker.info.backend_pid)
            clock[0] += timedelta(microseconds=1)
        assert pending.result(timeout=5).status_code == 401

    # 到期撤銷仍會提交，已保存的回答與模型用量也仍保留。
    assert private_reads == []
    assert len(provider_script.requests) == 1
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM login_sessions").fetchone() == (0,)
        status = conn.execute("SELECT status FROM answer_attempts").fetchone()
        assert status == ("insufficient_evidence",)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)


@pytest.mark.parametrize("readback", [False, True])
# 最後一筆私人 SQL 完成後登入到期，已送出仍計次，未提交的回答須回滾。
def test_absolute_session_expiry_during_final_answer_sql(
    answer_ready: Settings,
    provider_script: ProviderScript,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
    readback: bool,
) -> None:
    assert import_batch(monkeypatch, ORIGINAL) == 0
    provider_script.responses.extend(
        [interaction("requires_action", CALL), model_output("私人回答。", [2])]
    )
    actual = Connection.execute
    last_sql = (
        "SELECT q.conversation_id, q.id, a.status"
        if readback
        else "UPDATE conversations SET last_seen_at = GREATEST(last_seen_at, %s) WHERE id = %s"
    )

    # 保留實際 PostgreSQL 查詢和交易，於終態查詢或活動時間寫入後推進時鐘。
    def after_execute(
        self: Connection[tuple[object, ...]],
        query: str | bytes | SQL | Composed,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool = False,
    ) -> Cursor[tuple[object, ...]]:
        cursor = actual(self, query, params, prepare=prepare, binary=binary)
        if last_sql in str(query):
            clock[0] += timedelta(microseconds=1)
        return cursor

    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        set_session_near_absolute_expiry(answer_ready, clock[0])
        if readback:
            result = ask(browser)
            assert result.status_code == 200 and result.json()["answer"] == "私人回答。"
        monkeypatch.setattr(Connection, "execute", after_execute)
        denied = (
            browser.get(f"{ANSWERS}/{result.json()['attempt_id']}")
            if readback
            else ask(browser)
        )
        assert denied.status_code == 401 and "私人回答" not in denied.text

    # 拒絕不抹除已提交的工具證據，也不會寫入新的私人回答、引用或模型前文。
    assert len(provider_script.requests) == 2
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM login_sessions").fetchone() == (0,)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (3,)
        row = conn.execute(
            "SELECT status, answer, search_results, citations, model_steps IS NULL FROM answer_attempts"
        ).fetchone()
        if readback:
            assert row == (
                "answered",
                "私人回答。",
                ORIGINAL_RESULTS["results"],
                [ORIGINAL_RESULTS["results"][1]],
                False,
            )
        else:
            assert row == ("stopped", None, ORIGINAL_RESULTS["results"], [], True)


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("same_session", [False, True])
# 第一題仍在等終態列鎖時，任何工作階段都不能趁鎖釋放接著執行。
def test_concurrent_post_remains_busy_during_final_write(
    answer_ready: Settings,
    provider_script: ProviderScript,
    retry: bool,
    same_session: bool,
) -> None:
    waiting, release = Event(), Event()

    # 先等第一個模型請求送完，再扣住其嘗試列，讓正式完成交易阻塞在該列。
    async def pending() -> httpx.Response:
        waiting.set()
        assert await anyio.to_thread.run_sync(release.wait, 5)
        return model_output(None, [])

    provider_script.responses.extend(
        [pending, model_output(None, []), model_output(None, [])]
    )
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as first,
        TestClient(create_app(answer_ready), base_url=ORIGIN) as second,
        ThreadPoolExecutor(2) as pool,
    ):
        login(first)
        login(second)
        competitor = first if same_session else second
        original = pool.submit(ask, first)
        try:
            assert waiting.wait(4)
            with answer_ready.connect() as blocker:
                row = blocker.execute(
                    """
                    SELECT a.id, q.id, q.conversation_id FROM answer_attempts a
                    JOIN questions q ON q.id = a.question_id FOR UPDATE OF a
                    """
                ).fetchone()
                assert row is not None
                attempt_id, question_id, conversation_id = map(str, row)
                release.set()
                wait_for_search_lock(answer_ready, blocker.info.backend_pid)
                body = {"conversation_id": conversation_id}
                if retry:
                    body.update(question_id=question_id, attempt_id=attempt_id)
                else:
                    body["question"] = "並行追問"
                duplicate = pool.submit(
                    competitor.post, ANSWERS, headers=CSRF, json=body
                )

                # 首次交易持有對話鎖直到離開區塊。若舊行為開始排隊，先記錄後放鎖，讓測試得到真正的錯誤回應。
                with answer_ready.connect() as observer:
                    observer.autocommit = True
                    limit = monotonic() + 2
                    waited_query = (
                        "%SELECT username, created_at, last_seen_at FROM login_sessions%"
                        if same_session
                        else "%SELECT created_at, last_seen_at, key_version FROM conversations%"
                    )
                    while not duplicate.done():
                        blocked = observer.execute(
                            """
                            SELECT EXISTS (SELECT 1 FROM pg_stat_activity
                                WHERE datname = current_database() AND query LIKE %s
                                    AND cardinality(pg_blocking_pids(pid)) > 0)
                            """,
                            (waited_query,),
                        ).fetchone()
                        if blocked == (True,):
                            break
                        assert monotonic() < limit
            assert original.result(timeout=5).status_code == 200
            denied = duplicate.result(timeout=5)
            assert denied.status_code == 409
            assert denied.json() == {"detail": "這段對話正在處理問題，請稍後再試。"}
            assert len(provider_script.requests) == 1
            with answer_ready.connect() as conn:
                assert conn.execute("SELECT count(*) FROM questions").fetchone() == (1,)
                assert conn.execute(
                    "SELECT count(*) FROM answer_attempts"
                ).fetchone() == (1,)
                assert conn.execute(
                    "SELECT count(*) FROM model_requests"
                ).fetchone() == (2,)

            # 終態提交後明確再送一次可受理，證明忙碌拒絕未遺留狀態。
            accepted = competitor.post(ANSWERS, headers=CSRF, json=body)
            assert accepted.status_code == 200
            assert accepted.json()["attempt_id"] != attempt_id
            assert (accepted.json()["question_id"] == question_id) is retry
        finally:
            release.set()
    assert len(provider_script.requests) == 2


@pytest.mark.parametrize("precheck", [False, True])
# 初始化與額度預留或忙碌快查真實重疊，兩邊都能完成且保留正確額度。
def test_startup_during_business_quota_admission(
    answer_ready: Settings,
    provider_script: ProviderScript,
    monkeypatch: pytest.MonkeyPatch,
    precheck: bool,
) -> None:
    reached, go = Event(), Event()
    backend_pids: list[int] = []
    original_hint = answer_storage.busy_at_arrival

    # 等到初始化真的受到目前交易阻擋才放行，避免靠執行速度猜測重疊。
    def pause(conn: Connection[tuple[object, ...]]) -> None:
        backend_pids.append(conn.info.backend_pid)
        reached.set()
        assert go.wait(5)

    # 實際取得對話及執行列鎖後暫停，讓初始化確實等待這筆交易持有的表鎖。
    def held(
        conn: Connection[tuple[object, ...]], attempt: Attempt, deadline: float
    ) -> AttemptWindow | None:
        window = lock_running_attempt(conn, attempt, deadline)
        pause(conn)
        return window

    # 只在已讀到忙碌提示後暫停；快查交易結束應先釋放表鎖再驗證登入。
    def held_hint(
        conn: Connection[tuple[object, ...]], digest: bytes, conversation: UUID
    ) -> bool:
        busy = original_hint(conn, digest, conversation)
        pause(conn)
        return busy

    business_calls = 2 if precheck else 1
    provider_script.responses.extend(
        model_output(None, []) for _ in range(business_calls)
    )
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as browser,
        ThreadPoolExecutor(2) as pool,
    ):
        login(browser)
        if precheck:
            first = ask(browser)
            assert first.status_code == 200
            monkeypatch.setattr(answer_storage, "busy_at_arrival", held_hint)
            answer = pool.submit(
                browser.post,
                ANSWERS,
                headers=CSRF,
                json={
                    "question": QUESTION,
                    "conversation_id": first.json()["conversation_id"],
                },
            )
        else:
            monkeypatch.setattr(answers, "lock_running_attempt", held)
            answer = pool.submit(ask, browser)
        try:
            assert reached.wait(4)
            init = pool.submit(initialize, answer_ready)
            wait_for_search_lock(answer_ready, backend_pids[0])
        finally:
            go.set()
        result = answer.result(timeout=6)
        init.result(timeout=6)
        assert (
            result.status_code == 200
            and result.json()["status"] == "insufficient_evidence"
        )

    assert len(provider_script.requests) == business_calls
    with answer_ready.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (
            business_calls + 1,
        )
        assert (
            conn.execute(
                "SELECT delivery FROM model_requests WHERE username = 'user'"
            ).fetchall()
            == [("sent",)] * business_calls
        )


# B 初次讀到 ready 後暫停，若 A 的拒絕先提交，B 不能寫出任何 HTTP 位元組。
def test_rejection_before_final_admission_prevents_socket_write(
    answer_ready: Settings,
    provider_script: ProviderScript,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a_waiting, reject_a, b_initially_ready, resume_b = (
        Event(),
        Event(),
        Event(),
        Event(),
    )
    writes_after_a: list[bool] = []
    original_authorize = answer_storage.authorize

    # A 已完成真正的 HTTP 請求寫入；測試稍後才交回供應商的明確拒絕。
    async def rejection() -> httpx.Response:
        a_waiting.set()
        assert await anyio.to_thread.run_sync(reject_a.wait, 5)
        return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})

    # 真實授權完成後才暫停 B，讓 A 有機會在沒有啟用排他鎖時提交拒絕。
    def pause_after_ready(
        conn: Connection[tuple[object, ...]],
        settings: Settings,
        digest: bytes,
        *,
        refresh: bool = False,
        deadline: float | None = None,
    ) -> Access | answer_storage.AnswerDenied:
        result = original_authorize(
            conn, settings, digest, refresh=refresh, deadline=deadline
        )
        if (
            isinstance(result, Access)
            and result.identity.username == "support"
            and deadline is not None
            and not b_initially_ready.is_set()
        ):
            b_initially_ready.set()
            assert resume_b.wait(5)
        return result

    # 記錄進入實際 HTTP/1.1 串流寫入的次數，能區分還沒讀回應與完全未送出。
    async def observe_write() -> None:
        if a_waiting.is_set():
            writes_after_a.append(True)

    monkeypatch.setattr(answer_storage, "authorize", pause_after_ready)
    provider_script.before_write = observe_write
    provider_script.responses.extend((rejection, model_output(None, [])))
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as user,
        TestClient(create_app(answer_ready), base_url=ORIGIN) as support,
        ThreadPoolExecutor(2) as pool,
    ):
        login(user)
        login(support, SUPPORT_FORM)
        a = pool.submit(ask, user)
        assert a_waiting.wait(5)
        b = pool.submit(ask, support)
        try:
            assert b_initially_ready.wait(5)
            reject_a.set()
            assert a.result(timeout=5).status_code == 503
            assert user.get("/api/setup").json()["status"] == "invalid"
        finally:
            # 成功或失敗都釋放模擬供應商與 B 的工作執行緒。
            reject_a.set()
            resume_b.set()
        denied = b.result(timeout=5)
        assert denied.status_code == 503
        assert "search_results" not in denied.json()

    # 不但沒有第二次模型回覆，連第一個 HTTP 寫入動作也沒有；確定未送出才退額。
    assert writes_after_a == []
    assert len(provider_script.requests) == 1
    with answer_ready.connect() as conn:
        assert conn.execute(
            "SELECT delivery FROM model_requests WHERE username = 'support'"
        ).fetchone() == ("unsent",)
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)


# B 已獲准並進入寫入時，A 的拒絕須等本機寫完，但不能等 B 的供應商回覆。
def test_admitted_socket_write_finishes_before_rejection_without_holding_response(
    answer_ready: Settings,
    provider_script: ProviderScript,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a_waiting, reject_a, b_writing, write_b, b_waiting, reply_b = (
        Event() for _ in range(6)
    )
    dispatch_pid: list[int] = []

    # 第一筆模型回覆用於觸發同一個 Key 與驗證序號的拒絕。
    async def rejection() -> httpx.Response:
        a_waiting.set()
        assert await anyio.to_thread.run_sync(reject_a.wait, 5)
        return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})

    # 讓 B 已完成寫入但還沒收到回應時，測試拒絕是否已生效。
    async def delayed_reply() -> httpx.Response:
        b_waiting.set()
        assert await anyio.to_thread.run_sync(reply_b.wait, 5)
        return model_output(None, [])

    # 只停在 B 的第一個實際串流寫入點，不以高階 SDK 替身取代 transport。
    async def pause_write() -> None:
        if a_waiting.is_set() and not b_writing.is_set():
            b_writing.set()
            assert await anyio.to_thread.run_sync(write_b.wait, 5)

    # 記錄實際持有授權交易的 PostgreSQL PID，供測試觀察拒絕交易確實被它阻擋。
    def observe_dispatch(
        settings: Settings,
        digest: bytes,
        attempt: Attempt,
        deadline: float,
    ) -> Dispatch:
        dispatch = lock_dispatch(settings, digest, attempt, deadline)
        if attempt.access.identity.username == "support":
            dispatch_pid.append(dispatch[0].info.backend_pid)
        return dispatch

    monkeypatch.setattr(answers, "lock_dispatch", observe_dispatch)
    provider_script.before_write = pause_write
    provider_script.responses.extend((rejection, delayed_reply))
    with (
        TestClient(create_app(answer_ready), base_url=ORIGIN) as user,
        TestClient(create_app(answer_ready), base_url=ORIGIN) as support,
        ThreadPoolExecutor(2) as pool,
    ):
        login(user)
        login(support, SUPPORT_FORM)
        a = pool.submit(ask, user)
        assert a_waiting.wait(5)
        b = pool.submit(ask, support)
        try:
            assert b_writing.wait(5)
            reject_a.set()
            wait_for_search_lock(answer_ready, dispatch_pid[0])
            assert user.get("/api/setup").json()["status"] == "ready"
            write_b.set()
            assert b_waiting.wait(5)
            assert a.result(timeout=5).status_code == 503
            assert user.get("/api/setup").json()["status"] == "invalid"
            assert ask(user).status_code == 503
        finally:
            # 斷言失敗也釋放兩次請求的所有受控網路階段。
            reject_a.set()
            write_b.set()
            reply_b.set()
        assert b.result(timeout=5).status_code == 503

    # B 已完成 HTTP 寫入，拒絕後不能退回已用掉的兩筆業務額度。
    assert len(provider_script.requests) == 2
    with answer_ready.connect() as conn:
        assert conn.execute(
            "SELECT delivery FROM model_requests WHERE username IS NOT NULL"
        ).fetchall() == [("sent",), ("sent",)]
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (3,)
