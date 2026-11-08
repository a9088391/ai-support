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
from uuid import uuid4

import anyio
import httpcore
import httpx
import pytest
from backend import answer_storage, answers, cli, knowledge, sessions
from backend.answer_storage import (
    Access,
    Attempt,
    Dispatch,
    lock_dispatch,
    search_attempt,
)
from backend.app import create_app
from backend.credentials import set_key
from backend.database import Settings, reserve_request
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
from psycopg import Connection
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
        settings: Settings, digest: bytes, access: Access, *args: float
    ) -> Dispatch:
        result = original(settings, digest, access, *args)
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
        expected: Access,
        deadline: float,
    ) -> Dispatch:
        dispatch = lock_dispatch(settings, digest, expected, deadline)
        if expected.identity.username == "support":
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
