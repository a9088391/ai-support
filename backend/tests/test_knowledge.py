import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from time import monotonic
from typing import cast

import httpx
import pytest
from backend import cli, knowledge, sessions
from backend.app import create_app
from backend.credentials import set_key
from backend.database import Settings
from backend.tests.test_activation import success
from backend.tests.test_sessions import (
    CSRF,
    FIXTURE_KEY,
    ORIGIN,
    SUPPORT_FORM,
    USER_FORM,
    ProviderFixture,
    replay_cookie,
)
from backend.tests.test_sessions import client as client
from backend.tests.test_sessions import clock as clock
from backend.tests.test_sessions import ready as ready
from fastapi.testclient import TestClient

SEARCH = "/api/knowledge/search"
SEARCH_INVALID = {"detail": "搜尋欄位格式不正確。"}
ORIGINAL = {
    "documents": [
        {
            "id": "returns",
            "title": "退貨辦法",
            "version": "v1",
            "fragments": [{"id": "policy", "text": "退貨時請準備訂單號。"}],
        },
        {
            "id": "delivery",
            "title": "EXAMPLE-100 配送說明",
            "version": "v1",
            "fragments": [
                {"id": "time", "text": "一般配送需兩個工作天。"},
                {"id": "note", "text": "配送進度可在工作台查詢。符號：100%_ok。"},
            ],
        },
    ]
}
UPDATED_FRAGMENT = {"id": "pickup", "text": "配送可改到站取貨。"}
UPDATED_DOCUMENT = {
    "id": "delivery",
    "title": "配送說明",
    "version": "v2",
    "fragments": [UPDATED_FRAGMENT],
}
UPDATED = {"documents": [UPDATED_DOCUMENT]}
NEXT_BATCH = {
    "documents": [
        {
            "id": "next",
            "title": "配送通知",
            "version": "v3",
            "fragments": [{"id": "notice", "text": "配送將寄出通知。"}],
        }
    ]
}
ORIGINAL_RESULTS = {
    "results": [
        {
            "document_id": "delivery",
            "title": "EXAMPLE-100 配送說明",
            "version": "v1",
            "fragment_id": "note",
            "excerpt": "配送進度可在工作台查詢。符號：100%_ok。",
        },
        {
            "document_id": "delivery",
            "title": "EXAMPLE-100 配送說明",
            "version": "v1",
            "fragment_id": "time",
            "excerpt": "一般配送需兩個工作天。",
        },
    ]
}
UPDATED_RESULTS = {
    "results": [
        {
            "document_id": "delivery",
            "title": "配送說明",
            "version": "v2",
            "fragment_id": "pickup",
            "excerpt": "配送可改到站取貨。",
        }
    ]
}


# 經 CLI 真實 stdin 解析與寫入；原始 bytes 可保留壞 UTF-8 及重複欄位。
def import_batch(monkeypatch: pytest.MonkeyPatch, batch: object) -> int:
    encoded = batch if isinstance(batch, bytes) else json.dumps(batch).encode("utf-8")
    with (
        io.TextIOWrapper(io.BytesIO(encoded)) as stream,
        monkeypatch.context() as patch,
    ):
        patch.setattr("sys.stdin", stream)
        return cli.main(["import-knowledge"])


# 經同源 POST 送出查詢，仍讓呼叫端分別斷言狀態與來源欄位。
def search(browser: TestClient, query: str) -> httpx.Response:
    return cast(
        httpx.Response, browser.post(SEARCH, headers=CSRF, json={"query": query})
    )


# 同一入口驗證啟用、來源與兩個帳號；登出後原憑證也不可重送。
def test_knowledge_access(
    settings: Settings,
    mocked_provider: ProviderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 首匯可在尚未啟用時執行，但 HTTP 仍無法讀取已匯入的資料。
    requests, responses = mocked_provider
    with TestClient(create_app(settings), base_url=ORIGIN) as browser:
        assert import_batch(monkeypatch, ORIGINAL) == 0
        assert search(browser, "配送").status_code == 503
        set_key(settings.key_file, FIXTURE_KEY)
        assert search(browser, "配送").status_code == 503

        # 啟用後仍須登入；同源檢查拒絕欠缺標頭、跨站來源和沒有 Origin 的 POST。
        responses.append(success)
        assert cli.main(["verify-key"]) == 0
        assert search(browser, "配送").status_code == 401
        for headers in (
            {"Origin": ORIGIN},
            {**CSRF, "Origin": "https://invalid.example"},
            {"X-CSRF-Protection": "1"},
        ):
            assert (
                browser.post(
                    SEARCH, headers=headers, json={"query": "配送"}
                ).status_code
                == 403
            )

        # 兩個既有角色取得相同的來源資料；登出後舊憑證在後端已撤銷。
        for form in (USER_FORM, SUPPORT_FORM):
            login = browser.post("/api/login", headers=CSRF, json=form)
            assert login.status_code == 200
            assert login.json()["role"] == form["username"]
            result = search(browser, "配送")
            assert result.status_code == 200
            assert result.json() == ORIGINAL_RESULTS
            assert result.headers["cache-control"] == "no-store"
            assert browser.post("/api/logout", headers=CSRF).status_code == 204
            assert (
                browser.post(
                    SEARCH,
                    headers={
                        **CSRF,
                        **replay_cookie(login.cookies[sessions.COOKIE_NAME]),
                    },
                    json={"query": "配送"},
                ).status_code
                == 401
            )

        # 已發出的 Cookie 在設定被撤回後也不可搜尋。
        assert (
            browser.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
        )
        with settings.connect() as conn:
            conn.execute("UPDATE activation SET status = 'invalid'")
        assert search(browser, "配送").status_code == 503

    # 所有查詢只使用 PostgreSQL，不增加模型外呼及持久化額度。
    assert len(requests) == 1
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchall() == [(1,)]
        assert conn.execute("SELECT count(*) FROM model_requests").fetchone() == (1,)


# 觀察真正的 PostgreSQL 鎖等待，確定搜尋已通過入口檢查才改變條件。
def wait_for_search_lock(settings: Settings, blocker_pid: int) -> None:
    with settings.connect() as observer:
        observer.autocommit = True
        deadline = monotonic() + 5
        while observer.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_stat_activity
                WHERE datname = current_database()
                  AND %s = ANY(pg_blocking_pids(pid))
            )
            """,
            (blocker_pid,),
        ).fetchone() != (True,):
            assert monotonic() < deadline


# 搜尋通過入口後若在等待 Session 時失去啟用資格，必須拒絕且保留活動時間。
def test_search_rechecks_activation_after_waiting_for_session(
    client: TestClient,
    ready: Settings,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 以實際匯入與登入建立可搜尋的文件和有效 Session。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    assert client.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
    original_time = clock[0]
    clock[0] += timedelta(minutes=1)

    # 持住 Session 鎖，確認搜尋已通過入口並在 PostgreSQL 等待同一列。
    with ThreadPoolExecutor(1) as workers:
        with ready.connect() as blocker:
            blocker.execute("SELECT token_digest FROM login_sessions FOR UPDATE")
            searching = workers.submit(search, client, "配送")
            wait_for_search_lock(ready, blocker.info.backend_pid)

            # 先提交啟用失效，再讓搜尋交易取得 Session 鎖。
            with ready.connect() as conn:
                conn.execute("UPDATE activation SET status = 'invalid'")

        # 失效回覆不可帶文件，且搜尋不能延長這個 Session。
        denied = searching.result(timeout=5)
        assert denied.status_code == 503
        assert denied.json() == {
            "detail": "Google 已拒絕目前的憑證。請由本機操作人員更換 Key 並重新驗證。"
        }
    with ready.connect() as conn:
        assert conn.execute("SELECT last_seen_at FROM login_sessions").fetchone() == (
            original_time,
        )

        # 恢復相同綁定後，原 Session 仍可搜尋並刷新。
        conn.execute("UPDATE activation SET status = 'ready'")
    assert search(client, "配送").json() == ORIGINAL_RESULTS
    with ready.connect() as conn:
        assert conn.execute("SELECT last_seen_at FROM login_sessions").fetchone() == (
            clock[0],
        )


@pytest.mark.parametrize("wait_seconds", [1, 2])
# 啟用列鎖等待後重新判斷八小時上限，涵蓋仍有效的一秒與剛好到期。
def test_search_rechecks_expiry_after_waiting_for_activation(
    client: TestClient,
    ready: Settings,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
    wait_seconds: int,
) -> None:
    # 建立有效登入，將時鐘移到絕對期限前兩秒，並排除閒置到期。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    assert client.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
    clock[0] += sessions.SESSION_MAX - timedelta(seconds=2)
    with ready.connect() as conn:
        conn.execute(
            "UPDATE login_sessions SET last_seen_at = %s",
            (clock[0] - timedelta(minutes=1),),
        )

    # 確認搜尋正等啟用列鎖，才推進時鐘並釋放列鎖。
    with ThreadPoolExecutor(1) as workers:
        with ready.connect() as blocker:
            blocker.execute(
                "SELECT singleton FROM activation WHERE singleton FOR UPDATE"
            )
            searching = workers.submit(search, client, "配送")
            wait_for_search_lock(ready, blocker.info.backend_pid)
            clock[0] += timedelta(seconds=wait_seconds)
        result = searching.result(timeout=5)

    # 解鎖後仍有效才可取得片段並刷新。期限相等則提交刪除且不回傳任何片段。
    with ready.connect() as conn:
        last_seen = conn.execute("SELECT last_seen_at FROM login_sessions").fetchone()
    if wait_seconds == 1:
        assert result.status_code == 200
        assert result.json() == ORIGINAL_RESULTS
        assert last_seen == (clock[0],)
    else:
        assert result.status_code == 401
        assert result.json() == {"detail": "請先登入。"}
        assert last_seen is None


@pytest.mark.parametrize("absolute", [False, True])
# 合法查詢刷新閒置時間；非法查詢不刷新，兩種期限相等皆失效且提交刪除。
def test_knowledge_expiry(
    client: TestClient,
    ready: Settings,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
    absolute: bool,
) -> None:
    # 在閒置期限前一秒主動搜尋，使用真實資料庫確認新活動時間。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    login = client.post("/api/login", headers=CSRF, json=USER_FORM)
    assert login.status_code == 200
    remaining = sessions.SESSION_IDLE
    if absolute:
        # 只調整已存活的時間，讓八小時比閒置期限早一秒到期。
        remaining -= timedelta(seconds=1)
        with ready.connect() as conn:
            conn.execute(
                "UPDATE login_sessions SET created_at = %s",
                (clock[0] - sessions.SESSION_MAX + remaining,),
            )
    else:
        clock[0] += sessions.SESSION_IDLE - timedelta(seconds=1)
    assert search(client, "配送").json() == ORIGINAL_RESULTS
    with ready.connect() as conn:
        assert conn.execute("SELECT last_seen_at FROM login_sessions").fetchone() == (
            clock[0],
        )

    # 尚有一秒時，合法查詢仍可成功但不能延長最長期限，壞輸入也不能刷新閒置。
    clock[0] += remaining - timedelta(seconds=1)
    if absolute:
        assert search(client, "配送").json() == ORIGINAL_RESULTS
    else:
        assert search(client, " ").status_code == 422

    # 到期不得讀取文件；提交刪除後回撥時鐘也不能讓憑證復活。
    clock[0] += timedelta(seconds=1)
    assert search(client, "配送").status_code == 401
    with ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM login_sessions").fetchone() == (0,)
    clock[0] -= timedelta(seconds=1)
    assert search(client, "配送").status_code == 401


# 查詢中的 PostgreSQL 錯誤須安全失敗，且搜尋的活動刷新也不應提交。
def test_search_database_failure(
    client: TestClient,
    ready: Settings,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 暫時移開表名以觸發資料庫錯誤，不需要停測試服務或破壞批次。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    assert client.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
    with ready.connect() as conn:
        conn.execute(
            "ALTER TABLE knowledge_documents RENAME TO fixture_hidden_knowledge"
        )
    try:
        # 對外只顯示固定訊息；失敗後仍保留上次成功登入的活動時間。
        original_time = clock[0]
        clock[0] += timedelta(minutes=1)
        failed = search(client, "配送")
        assert failed.status_code == 503
        assert failed.json() == {"detail": "目前無法搜尋知識文件。"}
        with ready.connect() as conn:
            assert conn.execute(
                "SELECT last_seen_at FROM login_sessions"
            ).fetchone() == (original_time,)
    finally:
        with ready.connect() as conn:
            conn.execute(
                "ALTER TABLE fixture_hidden_knowledge RENAME TO knowledge_documents"
            )

    # 復原表名即能用原憑證取得原來源。
    assert search(client, "配送").json() == ORIGINAL_RESULTS


# 字面搜尋必須能區分語義接近、樣式符號及真正來源；錯誤不得回顯輸入。
def test_literal_search(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 登入但未匯入時可搜尋，只是沒有來源；匯入後可按標題與片段找到來源。
    assert client.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
    empty = search(client, "配送")
    assert empty.status_code == 200
    assert empty.json() == {"results": []}
    assert import_batch(monkeypatch, ORIGINAL) == 0
    assert search(client, "  example-100  ").json() == ORIGINAL_RESULTS
    for query in ("作天", "工作天"):
        assert search(client, query).json() == {
            "results": ORIGINAL_RESULTS["results"][1:]
        }
    for query in ("%_", "%", "_"):
        assert search(client, query).json() == {
            "results": ORIGINAL_RESULTS["results"][:1]
        }
    for query in ("送貨", "EXAMPLE%100", "退 貨", "' OR 1=1 --", "中" * 2000):
        assert search(client, query).json() == {"results": []}

    # 直接送原始本文，保留 JSON 重複名稱與無法傳入 PostgreSQL 的碼位。
    for body in (
        b"{}",
        b'{"query":7}',
        b'{"query":null}',
        b'{"query":""}',
        b'{"query":"secret","extra":"secret"}',
        b'{"query":"secret","query":"text"}',
        rb'{"query":"x\u0000"}',
        rb'{"query":"\ud800"}',
        b'{"query":"\xff"}',
        b"[" * 1200 + b"]" * 1200,
        b'{"query":' + b"9" * 5000 + b"}",
        json.dumps({"query": "中" * 2001}).encode("utf-8"),
    ):
        result = client.post(
            SEARCH, headers={**CSRF, "Content-Type": "application/json"}, content=body
        )
        assert result.status_code == 422
        assert result.json() == SEARCH_INVALID


# 多份文件命中時仍依來源順序取前 20 筆，與匯入順序無關。
def test_search_caps_sorted_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 刻意從高 ID 排到低 ID，讓截斷前是否排序可觀察。
    documents = [
        {
            "id": document_id,
            "title": "檢查順序",
            "version": "v1",
            "fragments": [
                {"id": f"p-{n:02d}", "text": "片段文字"} for n in range(11, 0, -1)
            ],
        }
        for document_id in ("z", "a")
    ]
    assert import_batch(monkeypatch, {"documents": documents}) == 0
    assert client.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200

    # 兩份文件共 22 筆；最後兩筆超出上限，前面的內容保持順序。
    response = search(client, "順序")
    assert response.status_code == 200
    assert [
        (result["document_id"], result["fragment_id"])
        for result in response.json()["results"]
    ] == [("a", f"p-{n:02d}") for n in range(1, 12)] + [
        ("z", f"p-{n:02d}") for n in range(1, 10)
    ]


# 匯入先驗證整批，資料庫途中拒絕時也必須復原舊批。
def test_import_replacement(
    client: TestClient,
    ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 匯入後取得同批來源；第二份資料不合法時，不得局部更新第一份。
    login = client.post("/api/login", headers=CSRF, json=USER_FORM)
    assert login.status_code == 200
    assert import_batch(monkeypatch, ORIGINAL) == 0
    invalid_batches: tuple[object, ...] = (
        {"documents": []},
        {"documents": [UPDATED_DOCUMENT, {"id": "missing-fixture"}]},
        {"documents": [UPDATED_DOCUMENT, UPDATED_DOCUMENT]},
        {"documents": [{**UPDATED_DOCUMENT, "fragments": [UPDATED_FRAGMENT] * 2}]},
        {"documents": [{**UPDATED_DOCUMENT, "id": "ascii\n"}]},
        {"documents": [{**UPDATED_DOCUMENT, "title": "　"}]},
        {"documents": [{**UPDATED_DOCUMENT, "version": 1}]},
        {"documents": [{**UPDATED_DOCUMENT, "fragments": []}]},
        {
            "documents": [
                {**UPDATED_DOCUMENT, "fragments": [{"id": "a", "text": "\x00"}]}
            ]
        },
        {
            "documents": [
                {**UPDATED_DOCUMENT, "fragments": [{"id": "a", "text": "\ud800"}]}
            ]
        },
        b'{"documents":[],"documents":[]}',
        b'{"documents": [\xff]}',
        b"[" * 1200 + b"]" * 1200,
        b'{"documents":' + b"9" * 5000 + b"}",
        json.dumps(UPDATED).encode("utf-8") + b" " * (4 * 1024 * 1024),
    )
    for batch in invalid_batches:
        assert import_batch(monkeypatch, batch) == 1
        assert search(client, "配送").json() == ORIGINAL_RESULTS
    output = capsys.readouterr()
    assert output.err.splitlines() == [
        "無法匯入知識文件：請檢查 JSON 格式、內容與大小。"
    ] * len(invalid_batches)

    # 強制 PostgreSQL 在新增時拒絕：前面的 DELETE 也必須回滾。
    with ready.connect() as conn:
        conn.execute(
            "ALTER TABLE knowledge_documents ADD CONSTRAINT fixture_version CHECK (version <> 'v2')"
        )
    try:
        assert import_batch(monkeypatch, UPDATED) == 1
        assert (
            capsys.readouterr().err
            == "無法匯入知識文件。請確認本機服務與資料庫狀態。\n"
        )
        assert search(client, "配送").json() == ORIGINAL_RESULTS
        assert len(search(client, "退貨").json()["results"]) == 1
    finally:
        with ready.connect() as conn:
            conn.execute(
                "ALTER TABLE knowledge_documents DROP CONSTRAINT fixture_version"
            )

    # 成功替換移除舊文件與片段；重跑與應用重啟仍維持相同版本。
    assert import_batch(monkeypatch, UPDATED) == 0
    assert import_batch(monkeypatch, UPDATED) == 0
    assert search(client, "配送").json() == UPDATED_RESULTS
    for query in ("退貨", "進度", "EXAMPLE-100"):
        assert search(client, query).json() == {"results": []}
    with TestClient(create_app(ready), base_url=ORIGIN) as restarted:
        restarted.cookies.set(sessions.COOKIE_NAME, login.cookies[sessions.COOKIE_NAME])
        assert search(restarted, "配送").json() == UPDATED_RESULTS


# 舊批已刪但尚未提交時，查詢可讀舊資料；兩個競爭匯入最後只保留一批。
def test_concurrent_imports_keep_complete_snapshot(
    client: TestClient, ready: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 以資料庫觸發器精確暫停在 DELETE 後、INSERT 前，方便觀察真正的交易順序。
    assert client.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
    assert import_batch(monkeypatch, ORIGINAL) == 0
    with ready.connect() as conn:
        conn.execute(
            """
            CREATE FUNCTION fixture_pause_import() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM pg_advisory_xact_lock(53003);
                RETURN NULL;
            END;
            $$;
            CREATE TRIGGER fixture_pause_import
                AFTER DELETE ON knowledge_documents FOR EACH STATEMENT
                EXECUTE FUNCTION fixture_pause_import();
            """
        )

    # 每個寫入者各用一個連線；觀察 PostgreSQL 實際進入指定的等待點。
    try:
        with ThreadPoolExecutor(3) as workers:
            with ready.connect() as observer:
                observer.autocommit = True
                observer.execute("SELECT pg_advisory_lock(53003)")
                try:
                    # 第一個寫入者進入觸發器後，舊資料才確定在未提交交易中刪除。
                    first = workers.submit(
                        knowledge.replace_documents,
                        ready,
                        knowledge.Batch.model_validate(UPDATED),
                    )
                    deadline = monotonic() + 5
                    while observer.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM pg_locks
                            WHERE locktype = 'advisory' AND classid = 0
                              AND objid = 53003 AND NOT granted
                        )
                        """
                    ).fetchone() != (True,):
                        assert monotonic() < deadline

                    # 另一個寫入者必須先等表鎖，不可進入自己的刪除階段。
                    second = workers.submit(
                        knowledge.replace_documents,
                        ready,
                        knowledge.Batch.model_validate(NEXT_BATCH),
                    )
                    deadline = monotonic() + 5
                    while observer.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM pg_locks
                            WHERE relation = 'knowledge_documents'::regclass
                              AND NOT granted
                        )
                        """
                    ).fetchone() != (True,):
                        assert monotonic() < deadline

                    # 第一批尚未提交、第二批等待時，HTTP 仍可直接取得完整原版本。
                    reading = workers.submit(search, client, "配送")
                    assert reading.result(timeout=5).json() == ORIGINAL_RESULTS
                finally:
                    # 即使斷言失敗也放行等待中的交易，避免卡住工作執行緒。
                    observer.execute("SELECT pg_advisory_unlock(53003)")

            # 解鎖後兩個交易依序提交。
            first.result(timeout=5)
            second.result(timeout=5)
    finally:
        # 移除臨時觸發器與函式，後續測試才不會再受到等待點影響。
        with ready.connect() as conn:
            conn.execute("DROP TRIGGER fixture_pause_import ON knowledge_documents")
            conn.execute("DROP FUNCTION fixture_pause_import()")

    # 第二個寫入者完整取代第一批，不會保留原始或中間片段。
    assert search(client, "配送").json() == {
        "results": [
            {
                "document_id": "next",
                "title": "配送通知",
                "version": "v3",
                "fragment_id": "notice",
                "excerpt": "配送將寄出通知。",
            }
        ]
    }


# 外部供應商的暫時拒絕保留 ready，也不得觸發搜尋改走模型。
def test_search_survives_provider_error(
    settings: Settings,
    mocked_provider: ProviderFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 經現有 SDK 傳輸邊界提供一次明確的供應商暫時錯誤。
    def unavailable() -> httpx.Response:
        return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})

    # 一次成功啟用、一次暫時失敗；兩次都計入額度。
    requests, responses = mocked_provider
    set_key(settings.key_file, FIXTURE_KEY)
    responses.extend((success, unavailable))
    assert cli.main(["verify-key"]) == 0
    assert cli.main(["verify-key"]) == 1
    assert import_batch(monkeypatch, ORIGINAL) == 0

    # 可繼續登入與取得來源，且搜尋不新增請求或配額。
    with TestClient(create_app(settings), base_url=ORIGIN) as browser:
        assert browser.get("/api/setup").json()["status"] == "ready"
        assert (
            browser.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
        )
        assert search(browser, "配送").json() == ORIGINAL_RESULTS
    assert len(requests) == 2
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchall() == [(2,)]


# 新增可選 metadata 不改變舊 JSONB；非法事實須在整批替換前拒絕。
def test_permission_import_preserves_legacy_and_validates_facts(
    ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 舊批次匯入後保留原欄位，不能因預設值多寫 null 或空陣列。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    with ready.connect() as conn:
        rows = conn.execute(
            "SELECT id, fragments FROM knowledge_documents ORDER BY id"
        ).fetchall()
    for document in ORIGINAL["documents"]:
        assert (document["id"], document["fragments"]) in rows

    # 嚴格型別、列舉與必填欄位均由現有 CLI 驗證，錯誤不替換舊資料。
    fact = {
        "role": "viewer",
        "action": "read",
        "resource": "shared_reports",
        "effect": "allow",
    }
    for permissions in (
        [{**fact, "effect": True}],
        [{**fact, "effect": "unknown"}],
        [{"role": "viewer"}],
        "allow",
    ):
        batch = {
            "documents": [
                {
                    **UPDATED_DOCUMENT,
                    "fragments": [{**UPDATED_FRAGMENT, "permissions": permissions}],
                }
            ]
        }
        assert import_batch(monkeypatch, batch) == 1
    with ready.connect() as conn:
        assert (
            conn.execute(
                "SELECT id, fragments FROM knowledge_documents ORDER BY id"
            ).fetchall()
            == rows
        )


@pytest.mark.parametrize(
    "recipient",
    [
        {"role": "owner"},
        {"role": "owner", "label": " "},
        {"role": "Owner", "label": "工作區擁有者"},
        {"role": "owner", "label": True},
        {"role": "owner", "label": "工作區擁有者", "unknown": True},
    ],
)
# 申請對象沿用嚴格匯入契約，缺欄、空白或型別錯誤都不能成為回答事實。
def test_application_recipient_validation(recipient: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        knowledge.Permission.model_validate(
            {
                "role": "viewer",
                "action": "modify",
                "resource": "workspace_settings",
                "effect": "deny",
                "application_recipient": recipient,
            }
        )


# 只有禁止事實能帶申請對象；舊事實匯出仍不增加 null 欄位。
def test_application_recipient_requires_denial_and_preserves_legacy() -> None:
    fact = {
        "role": "viewer",
        "action": "modify",
        "resource": "workspace_settings",
        "effect": "deny",
    }
    assert (
        knowledge.Permission.model_validate(fact).model_dump(exclude_none=True) == fact
    )
    recipient = {"role": "owner", "label": "工作區擁有者"}
    updated = fact | {"application_recipient": recipient}
    assert (
        knowledge.Permission.model_validate(updated).model_dump(exclude_none=True)
        == updated
    )
    with pytest.raises(ValueError):
        knowledge.Permission.model_validate(updated | {"effect": "allow"})
