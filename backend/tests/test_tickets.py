import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from operator import itemgetter
from threading import Barrier, Event
from time import monotonic
from typing import cast
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from backend import answers, knowledge
from backend.answer_storage import Attempt, AttemptDeadline, search_attempt
from backend.app import create_app
from backend.database import Settings
from backend.tests.test_answers import (
    ANSWERS,
    CALL,
    QUESTION,
    ProviderScript,
    ask,
    interaction,
    login,
    model_output,
    retry_form,
    set_session_near_absolute_expiry,
)
from backend.tests.test_answers import answer_ready as answer_ready
from backend.tests.test_answers import provider_script as provider_script
from backend.tests.test_knowledge import (
    ORIGINAL,
    ORIGINAL_RESULTS,
    UPDATED,
    UPDATED_RESULTS,
    import_batch,
    wait_for_search_lock,
)
from backend.tests.test_sessions import CSRF, ORIGIN, SUPPORT_FORM, replay_cookie
from backend.tests.test_sessions import clock as clock
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

TICKETS = "/api/tickets"
TICKET_SUMMARY_FIELDS = (
    "id",
    "description",
    "question",
    "created_at",
    "status",
    "resolution",
    "closed_at",
)


# 用實際已回答的問題建單，分別核對可編輯描述與可信回答和來源。
def test_ticket_snapshots_completed_answer(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
) -> None:
    # 透過知識匯入與模型傳輸建立可信證據。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    answer = "一般配送需兩個工作天。"
    provider_script.responses.extend(
        [interaction("requires_action", CALL), model_output(answer, [2])]
    )

    # 工單回應與後續明細都要包含相同的獨立證據。
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        result = ask(browser)
        assert result.status_code == 200
        attempt = result.json()
        form = {key: attempt[key] for key in ("question_id", "attempt_id")}
        description = "<script>補充配送背景</script>"
        response = browser.post(
            TICKETS, headers=CSRF, json={**form, "description": description}
        )
        assert response.status_code == 200
        ticket = response.json()
        assert UUID(ticket["id"])
        assert ticket == {
            **form,
            "id": ticket["id"],
            "created_at": ticket["created_at"],
            "created_by": "user",
            "status": "open",
            "resolution": None,
            "closed_at": None,
            "description": description,
            "question": QUESTION,
            "answer_status": "answered",
            "answer": answer,
            "citations": [ORIGINAL_RESULTS["results"][1]],
            "search_results": ORIGINAL_RESULTS["results"],
        }
        assert browser.get(f"{TICKETS}/{ticket['id']}").json() == ticket


# 建單輸入一律使用公開問答回應的識別，不附上任何證據欄位。
def ticket_form(result: httpx.Response, description: str) -> dict[str, str]:
    assert result.status_code == 200
    answer = result.json()
    return {
        "question_id": answer["question_id"],
        "attempt_id": answer["attempt_id"],
        "description": description,
    }


# 用 API 驗證角色可見範圍、來源、本文及純文字的 Unicode 字元邊界。
def test_ticket_http_rejects_invalid_input_and_other_owners(
    answer_ready: Settings, provider_script: ProviderScript
) -> None:
    # 建單前先用空 Session 驗證端點不接受匿名存取。
    app = create_app(answer_ready)
    provider_script.responses.append(model_output(None, []))
    with (
        TestClient(app, base_url=ORIGIN) as browser,
        TestClient(app, base_url=ORIGIN) as other,
    ):
        assert browser.get(TICKETS).status_code == 401
        login(browser)
        form = ticket_form(ask(browser), "🙂" * 4000)
        assert other.post(TICKETS, headers=CSRF, json=form).status_code == 401

        # 原始 JSON 保留重複鍵與畸形本文；所有驗證拒絕都只回固定文案。
        raw = json.dumps(form, ensure_ascii=False).encode("utf-8")
        assert "🙂".encode("utf-8") * 4000 in raw
        headers = {**CSRF, "Content-Type": "application/json"}
        for body in (raw[:-1], raw[:-1] + b', "description": "private-marker"}'):
            denied = browser.post(TICKETS, headers=headers, content=body)
            assert denied.status_code == 422
            assert denied.json() == {"detail": "工單欄位格式不正確。"}
        for fields in (
            {"answer": "private-marker"},
            {"description": "　\n"},
            {"description": form["description"] + "🙂"},
            {"attempt_id": "private-marker"},
        ):
            denied = browser.post(TICKETS, headers=CSRF, json={**form, **fields})
            assert denied.status_code == 422
            assert denied.json() == {"detail": "工單欄位格式不正確。"}
        assert browser.post(TICKETS, json=form).status_code == 403
        assert browser.get(TICKETS).json() == {"tickets": [], "has_more": False}

        # 正好 4000 個 Unicode 字元不截斷；客服可看快照但不能代替原建立人重送。
        created = browser.post(TICKETS, headers=headers, content=raw)
        assert created.status_code == 200
        ticket = created.json()
        assert ticket["description"] == form["description"]
        detail = f"{TICKETS}/{ticket['id']}"
        assert other.get(detail).status_code == 401
        login(other, SUPPORT_FORM)
        assert other.get(detail).json() == ticket
        assert other.post(TICKETS, headers=CSRF, json=form).status_code == 404
        assert (
            browser.get(f"{TICKETS}/private-marker").json()
            == other.get(f"{TICKETS}/{uuid4()}").json()
        )
        assert (
            browser.post(
                TICKETS, headers=CSRF, json={**form, "attempt_id": str(uuid4())}
            ).status_code
            == 404
        )

        # 失去啟用資格後，既有登入也不能讀取或重送工單。
        answer_ready.key_file.unlink()
        assert browser.get(TICKETS).status_code == 503
        assert browser.get(detail).status_code == 503
        assert browser.post(TICKETS, headers=CSRF, json=form).status_code == 503


# 驗證客服分頁包含兩個帳號的工單，查閱範圍只擴及快照。
def test_ticket_visibility_and_pagination_use_the_session_role(
    answer_ready: Settings, provider_script: ProviderScript, clock: list[datetime]
) -> None:
    # 兩個角色各自從 HTTP 問答建單，另保留一題一般帳號的未轉單問答。
    provider_script.responses.extend([model_output(None, []) for _ in range(3)])
    app = create_app(answer_ready)
    with (
        TestClient(app, base_url=ORIGIN) as browser,
        TestClient(app, base_url=ORIGIN) as support,
    ):
        login(browser)
        login(support, SUPPORT_FORM)
        form = ticket_form(ask(browser), "一般帳號工單")
        original = browser.post(TICKETS, headers=CSRF, json=form)
        assert original.status_code == 200
        user_ticket = original.json()
        unconverted = ticket_form(ask(browser), "尚未轉單")
        clock[0] += timedelta(seconds=1)
        created = support.post(
            TICKETS, headers=CSRF, json=ticket_form(ask(support), "客服工單")
        )
        assert created.status_code == 200
        support_ticket = created.json()

        # 核對客服兩頁的確切成員與順序，以及一般帳號的可見分頁邊界。
        support_summary = {key: support_ticket[key] for key in TICKET_SUMMARY_FIELDS}
        user_summary = {key: user_ticket[key] for key in TICKET_SUMMARY_FIELDS}
        assert support.get(f"{TICKETS}?limit=1").json() == {
            "tickets": [support_summary],
            "has_more": True,
        }
        other_page = support.get(f"{TICKETS}?limit=1&offset=1").json()

        # 客服清單的證據取自原始問題，與提單者編輯的描述分開。
        row = other_page["tickets"][0]
        assert (row.get("question"), row["description"]) == (QUESTION, "一般帳號工單")
        assert other_page == {
            "tickets": [user_summary],
            "has_more": False,
        }
        assert browser.get(f"{TICKETS}?limit=1").json() == {
            "tickets": [user_summary],
            "has_more": False,
        }
        assert browser.get(f"{TICKETS}?limit=1&offset=1").json() == {
            "tickets": [],
            "has_more": False,
        }
        assert support.get(f"{TICKETS}/{user_ticket['id']}").json() == user_ticket
        assert support.get(f"{TICKETS}/{support_ticket['id']}").json() == support_ticket
        other = browser.get(f"{TICKETS}/{support_ticket['id']}")
        absent = browser.get(f"{TICKETS}/{uuid4()}")
        assert other.status_code == absent.status_code == 404
        assert other.json() == absent.json()

        # 快照可見也不能拿它讀取原問答、重送他人已建工單或替未轉單問答建單。
        for source in (form, unconverted):
            assert support.get(f"{ANSWERS}/{source['attempt_id']}").status_code == 404
            assert support.post(TICKETS, headers=CSRF, json=source).status_code == 404


# 結案只接受客服與有效的純文字輸入，成功後提單者能看到完整處理結果。
def test_ticket_close_http_permissions_and_resolution_limits(
    answer_ready: Settings, provider_script: ProviderScript, clock: list[datetime]
) -> None:
    # 由一般帳號建立工單，保留未結案快照供後續比較。
    provider_script.responses.append(model_output(None, []))
    app = create_app(answer_ready)
    with (
        TestClient(app, base_url=ORIGIN) as browser,
        TestClient(app, base_url=ORIGIN) as support,
    ):
        login(browser)
        form = ticket_form(ask(browser), "原始描述")
        created = browser.post(TICKETS, headers=CSRF, json=form)
        assert created.status_code == 200
        ticket = created.json()
        detail = f"{TICKETS}/{ticket['id']}"
        close = f"{detail}/close"
        unknown = f"{TICKETS}/{uuid4()}/close"
        malformed = f"{TICKETS}/private-marker/close"
        text = "🙂" * 3998 + " \n"
        valid = {"resolution": text}

        # 登入、角色、CSRF 與識別的拒絕不洩漏目標是否存在或含有哪些私人欄位。
        assert support.post(close, headers=CSRF, json=valid).status_code == 401
        forbidden = browser.post(close, headers=CSRF, json=valid)
        assert forbidden.status_code == 403
        for path in (unknown, malformed):
            denied = browser.post(path, headers=CSRF, json=valid)
            assert denied.status_code == 403
            assert denied.json() == forbidden.json()
        login(support, SUPPORT_FORM)
        assert support.post(close, json=valid).status_code == 403
        missing = support.post(unknown, headers=CSRF, json=valid)
        assert missing.status_code == 404
        bad_id = support.post(malformed, headers=CSRF, json=valid)
        assert bad_id.status_code == 404
        assert bad_id.json() == missing.json()

        # 送出原始本文以保留重複鍵與不完整 JSON，並驗證 Unicode 字元界線。
        headers = {**CSRF, "Content-Type": "application/json"}
        raw = json.dumps(valid, ensure_ascii=False).encode("utf-8")
        oversized = json.dumps({"resolution": "🙂" + text}, ensure_ascii=False).encode(
            "utf-8"
        )
        assert len(text) == 4000 and "🙂".encode("utf-8") * 3998 in raw
        assert "🙂".encode("utf-8") * 3999 in oversized
        invalid_bodies = (
            raw[:-1],
            raw[:-1] + b', "resolution": "private-marker"}',
            json.dumps({**valid, "status": "closed"}).encode(),
            json.dumps({"resolution": "　\n"}).encode(),
            oversized,
            b'{"resolution":1}',
            b"{}",
        )
        for invalid in invalid_bodies:
            denied = support.post(close, headers=headers, content=invalid)
            assert denied.status_code == 422
            assert denied.json() == {"detail": "處理結果欄位格式不正確。"}
        invalid_probe = browser.post(malformed, headers=headers, content=b"{}")
        assert invalid_probe.status_code == 422
        assert invalid_probe.json() == denied.json()
        assert support.get(detail).json() == ticket

        # 正好 4000 字元保留原樣，狀態與時間在本人及客服的讀取回應一致。
        closed = support.post(close, headers=headers, content=raw)
        assert closed.status_code == 200
        saved = closed.json()
        closed_at = datetime.fromisoformat(saved["closed_at"])
        assert closed_at == clock[0] and closed_at.tzinfo == UTC
        assert saved == {
            **ticket,
            "status": "closed",
            "closed_at": saved["closed_at"],
            "resolution": text,
        }
        for client in (browser, support):
            assert client.get(detail).json() == saved
            assert client.get(TICKETS).json() == {
                "tickets": [{key: saved[key] for key in TICKET_SUMMARY_FIELDS}],
                "has_more": False,
            }

        # 失去啟用資格後，既有客服登入也不能異動工單。
        answer_ready.key_file.unlink()
        assert support.post(close, headers=CSRF, json=valid).status_code == 503


# 正常重啟沿用同一 PostgreSQL 與 Key，並保留開啟／結案工單及額度帳本。
def test_normal_restart_preserves_open_and_closed_tickets(
    answer_ready: Settings,
    provider_script: ProviderScript,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
) -> None:
    # 以同一讀取涵蓋啟用綁定、完整請求明細及每日使用量。
    def persisted_model_state() -> tuple[object, ...]:
        with answer_ready.connect() as conn:
            return (
                conn.execute(
                    """
                    SELECT key_version, fingerprint, provider, model, status,
                           probe_sequence, verified_sequence
                    FROM activation
                    """
                ).fetchone(),
                conn.execute(
                    """
                    SELECT id, service_day, username, delivery, rejected_key_version,
                           rejected_verified_sequence
                    FROM model_requests ORDER BY id
                    """
                ).fetchall(),
                conn.execute(
                    "SELECT service_day, used FROM daily_model_quota ORDER BY service_day"
                ).fetchall(),
            )

    # 分別記錄啟用與問答傳輸，讓重啟後能證明兩條路徑都沒有外呼。
    activation_requests, _ = mocked_provider
    provider_script.responses.extend([model_output(None, []), model_output(None, [])])

    # 經真實 HTTP 入口建立兩張票，再由客服端點只結案其中一張。
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as client:
        login(client)
        ready = client.get("/api/setup")
        assert ready.status_code == 200 and ready.json()["status"] == "ready"

        opened = client.post(
            TICKETS,
            headers=CSRF,
            json=ticket_form(ask(client), "重啟後仍開啟"),
        )
        assert opened.status_code == 200
        open_ticket = opened.json()
        assert open_ticket["status"] == "open"
        assert open_ticket["resolution"] is None and open_ticket["closed_at"] is None

        created = client.post(
            TICKETS,
            headers=CSRF,
            json=ticket_form(ask(client), "重啟後仍結案"),
        )
        assert created.status_code == 200
        ticket = created.json()
        login(client, SUPPORT_FORM)
        closed = client.post(
            f"{TICKETS}/{ticket['id']}/close",
            headers=CSRF,
            json={"resolution": "已完成處理"},
        )
        assert closed.status_code == 200
        closed_ticket = closed.json()
        assert closed_ticket["status"] == "closed"
        assert closed_ticket["resolution"] == "已完成處理"
        assert closed_ticket["closed_at"]

        # 把結案快照的來源留在有期限的執行中狀態，模擬程序中斷時的未完成問答。
        with answer_ready.connect() as conn:
            interrupted = conn.execute(
                """
                UPDATE answer_attempts
                SET status = 'running', deadline_at = clock_timestamp() + interval '1 minute'
                WHERE id = %s
                RETURNING status
                """,
                (closed_ticket["attempt_id"],),
            ).fetchone()
        assert interrupted == ("running",)
        assert client.get(f"{TICKETS}/{closed_ticket['id']}").json() == closed_ticket

        # 讀取重啟前的完整模型紀錄及兩個替身已送出的內容作為不變基準。
        before_restart = persisted_model_state()
        activation_sent = list(activation_requests)
        answers_sent = list(provider_script.requests)
        assert len(activation_sent) == 1
        assert len(answers_sent) == 2

    # 新 app 會重跑真實 FastAPI lifespan，並使用相同資料庫設定與 Key 路徑。
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as restarted:
        assert restarted.get("/api/setup").json() == ready.json()
        login(restarted)
        assert restarted.get(f"{TICKETS}/{open_ticket['id']}").json() == open_ticket
        assert restarted.get(f"{TICKETS}/{closed_ticket['id']}").json() == closed_ticket

    # 啟動、啟用狀態查詢與票務讀回均未改寫持久帳本或觸發任一模型傳輸。
    assert persisted_model_state() == before_restart
    assert activation_requests == activation_sent
    assert provider_script.requests == answers_sent


# 兩個獨立客服 Session 競爭同一張工單時，只保留勝出的結果及原快照。
def test_ticket_concurrent_closes_preserve_first_result(
    answer_ready: Settings, provider_script: ProviderScript
) -> None:
    # 由一般帳號建立快照，再開啟兩個獨立的客服登入。
    provider_script.responses.append(model_output(None, []))
    app = create_app(answer_ready)
    with (
        TestClient(app, base_url=ORIGIN) as browser,
        TestClient(app, base_url=ORIGIN) as first,
        TestClient(app, base_url=ORIGIN) as second,
    ):
        login(browser)
        assert login(first, SUPPORT_FORM) != login(second, SUPPORT_FORM)
        form = ticket_form(ask(browser), "原始描述")
        original = browser.post(TICKETS, headers=CSRF, json=form)
        assert original.status_code == 200
        ticket = original.json()
        detail = f"{TICKETS}/{ticket['id']}"
        close = f"{detail}/close"
        resolutions = ("處理結果一", "處理結果二")

        # 用資料庫列鎖保留兩個等待中的結案，觀察確實重疊後才放行。
        with ThreadPoolExecutor(max_workers=2) as executor:
            with answer_ready.connect() as blocker, answer_ready.connect() as observer:
                blocker.execute(
                    "SELECT id FROM tickets WHERE id = %s FOR NO KEY UPDATE",
                    (ticket["id"],),
                )
                observer.autocommit = True
                # 各自送出不同結果，讓兩筆獨立請求都等在工單鎖上。
                requests = [
                    executor.submit(
                        client.post,
                        close,
                        headers=CSRF,
                        json={"resolution": resolution},
                    )
                    for client, resolution in zip(
                        (first, second), resolutions, strict=True
                    )
                ]
                deadline = monotonic() + 2
                while observer.execute(
                    """
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database() AND wait_event_type = 'Lock'
                      AND query LIKE '%tickets%' AND cardinality(pg_blocking_pids(pid)) > 0
                    """
                ).fetchone() != (2,):
                    assert monotonic() < deadline
            responses = [pending.result(timeout=5) for pending in requests]

        # 勝出回應必須符合該次輸入；衝突回應不得帶出任一處理結果。
        codes = [response.status_code for response in responses]
        assert sorted(codes) == [200, 409]
        winner = responses[codes.index(200)].json()
        conflict = responses[codes.index(409)]
        assert all(resolution not in conflict.text for resolution in resolutions)
        assert winner == {
            **ticket,
            "status": "closed",
            "resolution": resolutions[codes.index(200)],
            "closed_at": winner["closed_at"],
        }
        assert datetime.fromisoformat(winner["closed_at"]).tzinfo == UTC

        # 重複結案仍衝突，原次建單重送則只讀回已保存的快照及結案結果。
        repeat = first.post(close, headers=CSRF, json={"resolution": "不應覆寫"})
        assert repeat.status_code == 409 and repeat.json() == conflict.json()
        assert browser.get(detail).json() == winner
        replay = browser.post(
            TICKETS, headers=CSRF, json={**form, "description": "重送"}
        )
        assert replay.status_code == 200 and replay.json() == winner


@pytest.mark.parametrize(
    ("block_write", "expire_session"), [(False, True), (True, True), (True, False)]
)
# 等待讀取或寫入期間若失去授權，拒絕結案並保留原工單。
def test_ticket_close_rechecks_authorization_after_waiting(
    answer_ready: Settings,
    provider_script: ProviderScript,
    clock: list[datetime],
    block_write: bool,
    expire_session: bool,
) -> None:
    # 建單後登出一般帳號，只讓客服的 Session 接近絕對期限。
    provider_script.responses.append(model_output(None, []))
    app = create_app(answer_ready)
    with (
        TestClient(app, base_url=ORIGIN) as browser,
        TestClient(app, base_url=ORIGIN) as support,
    ):
        login(browser)
        created = browser.post(
            TICKETS, headers=CSRF, json=ticket_form(ask(browser), "原始描述")
        )
        assert created.status_code == 200
        ticket = created.json()
        detail = f"{TICKETS}/{ticket['id']}"
        assert browser.post("/api/logout", headers=CSRF).status_code == 204
        login(support, SUPPORT_FORM)
        if expire_session:
            set_session_near_absolute_expiry(answer_ready, clock[0])

        # 列鎖阻塞讀取工單狀態，SHARE 表鎖則只擋 UPDATE，確保驗證兩個等待點。
        key_file = answer_ready.key_file
        hidden_key = key_file.with_suffix(".inactive")
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                with answer_ready.connect() as blocker:
                    if block_write:
                        blocker.execute("LOCK TABLE tickets IN SHARE MODE")
                    else:
                        blocker.execute("SELECT id FROM tickets FOR UPDATE")
                    result = executor.submit(
                        support.post,
                        f"{detail}/close",
                        headers=CSRF,
                        json={"resolution": "私人處理結果"},
                    )
                    wait_for_search_lock(answer_ready, blocker.info.backend_pid)

                    # 確認已進入交易才推進測試時鐘或暫時移走隔離憑證。
                    if expire_session:
                        clock[0] += timedelta(microseconds=1)
                    else:
                        key_file.rename(hidden_key)
                denied = result.result(timeout=5)
                assert denied.status_code == (401 if expire_session else 503)
                assert "私人處理結果" not in denied.text
            finally:
                # 回復測試憑證以驗證原工單仍可讀取。
                if hidden_key.exists():
                    hidden_key.rename(key_file)

        # 到期後撤銷實體 Session，並以提單者重新登入查核無任何結案欄位或快照被改動。
        if expire_session:
            with answer_ready.connect() as conn:
                assert conn.execute(
                    "SELECT count(*) FROM login_sessions"
                ).fetchone() == (0,)
        login(browser)
        assert browser.get(detail).json() == ticket


@pytest.mark.parametrize("has_ticket", [False, True])
# 驗證同一 Session 與不同 Session 的競爭建單或新識別重送都保留同一工單。
def test_ticket_concurrent_replays_keep_first_description(
    answer_ready: Settings, provider_script: ProviderScript, has_ticket: bool
) -> None:
    # 使用已啟動的同一個 ASGI 應用程式及兩個獨立登入。
    provider_script.responses.append(model_output(None, []))
    app = create_app(answer_ready)
    with (
        TestClient(app, base_url=ORIGIN) as browser,
        TestClient(app, base_url=ORIGIN) as other,
    ):
        first_cookie = login(browser)
        other_cookie = login(other)
        assert first_cookie != other_cookie
        source = ask(browser)
        original = None
        if has_ticket:
            # 有工單時先重試問答，讓併發請求爭用尚未登記的新識別。
            first = browser.post(
                TICKETS, headers=CSRF, json=ticket_form(source, "原描述")
            )
            assert first.status_code == 200
            original = first.json()
            provider_script.responses.append(model_output(None, []))
            source = browser.post(ANSWERS, headers=CSRF, json=retry_form(source.json()))
        form = ticket_form(source, "")

        # 三個 HTTP 工作先同時到齊，才能送出各自不同的描述。
        arrived = Barrier(3)

        # 使用明確 Cookie 避免不同執行緒互相修改瀏覽器的 Session。
        def submit(client: TestClient, cookie: str, description: str) -> httpx.Response:
            arrived.wait(timeout=5)
            return cast(
                httpx.Response,
                client.post(
                    TICKETS,
                    headers={**CSRF, **replay_cookie(cookie)},
                    json={**form, "description": description},
                ),
            )

        with ThreadPoolExecutor(max_workers=3) as executor:
            responses = [
                executor.submit(submit, client, cookie, description)
                for client, cookie, description in (
                    (browser, first_cookie, "描述一"),
                    (browser, first_cookie, "描述二"),
                    (other, other_cookie, "描述三"),
                )
            ]
            results = [response.result(timeout=5) for response in responses]
        assert [result.status_code for result in results] == [200, 200, 200]
        ticket = results[0].json()
        assert all(result.json() == ticket for result in results)
        if has_ticket:
            assert ticket == original
        else:
            assert ticket["description"] in {"描述一", "描述二", "描述三"}
        assert (
            browser.post(
                TICKETS, headers=CSRF, json={**form, "description": "重送"}
            ).json()
            == ticket
        )
        assert [row["id"] for row in browser.get(TICKETS).json()["tickets"]] == [
            ticket["id"]
        ]


# 已失敗問答的真實來源在重試、改版和清理後仍維持原樣。
def test_ticket_failure_snapshot_survives_retry_update_and_purge(
    answer_ready: Settings,
    provider_script: ProviderScript,
    monkeypatch: pytest.MonkeyPatch,
    clock: list[datetime],
) -> None:
    # 供應商的 503 私有內容不能成為公開證據；已完成的本地搜尋必須保留。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    waiting, release = Event(), Event()

    # 新嘗試停在供應商回覆邊界，讓重送發生在這次嘗試仍在執行時。
    async def pending() -> httpx.Response:
        waiting.set()
        assert await anyio.to_thread.run_sync(release.wait, 5)
        return model_output(None, [])

    provider_script.responses.extend(
        [
            interaction("requires_action", CALL),
            httpx.Response(503, json={"error": {"message": "private-marker"}}),
            model_output(None, []),
            pending,
            model_output(None, []),
            model_output(None, []),
            model_output(None, []),
        ]
    )
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        failed = ask(browser)
        form = ticket_form(failed, "問題未解決")
        created = browser.post(TICKETS, headers=CSRF, json=form)
        assert created.status_code == 200
        ticket = created.json()
        assert "private-marker" not in failed.text + created.text
        assert ticket["answer_status"] == failed.json()["status"] == "unavailable"
        assert ticket["answer"] is None and ticket["citations"] == []
        assert ticket["search_results"] == ORIGINAL_RESULTS["results"]

        # 原次重送或同一題的有效新嘗試都取得原工單；其他題的過時嘗試不能首次建單。
        new = browser.post(ANSWERS, headers=CSRF, json=retry_form(failed.json()))
        replay = ticket_form(new, "新版描述")
        accepted = browser.post(TICKETS, headers=CSRF, json=replay)
        assert accepted.status_code == 200 and accepted.json() == ticket

        # 重送上一個已驗證的識別，不應停止正在執行的新嘗試。
        with ThreadPoolExecutor(max_workers=1) as executor:
            in_progress = executor.submit(
                browser.post, ANSWERS, headers=CSRF, json=retry_form(new.json())
            )
            try:
                assert waiting.wait(5)
                later_replays = [browser.post(TICKETS, headers=CSRF, json=replay)]
            finally:
                release.set()
            current = in_progress.result(timeout=5)
        assert current.status_code == 200

        # 同一題未登記過的嘗試一旦被取代，仍須拒絕其重送。
        latest = browser.post(ANSWERS, headers=CSRF, json=retry_form(current.json()))
        assert latest.status_code == 200
        assert (
            browser.post(
                TICKETS, headers=CSRF, json=ticket_form(current, "未登記的舊嘗試")
            ).status_code
            == 409
        )
        old = ask(browser)
        newer = browser.post(ANSWERS, headers=CSRF, json=retry_form(old.json()))
        assert newer.status_code == 200
        assert (
            browser.post(
                TICKETS, headers=CSRF, json=ticket_form(old, "過期嘗試")
            ).status_code
            == 409
        )
        assert (
            browser.post(
                TICKETS,
                headers=CSRF,
                json={**form, "attempt_id": newer.json()["attempt_id"]},
            ).status_code
            == 404
        )

        # 改版確實作用於新搜尋；工單仍使用既有的版本與原始描述。
        assert import_batch(monkeypatch, UPDATED) == 0
        assert (
            browser.post(
                "/api/knowledge/search", headers=CSRF, json={"query": "配送"}
            ).json()
            == UPDATED_RESULTS
        )
        clock[0] += timedelta(minutes=30)
        login(browser)
        later_replays.append(browser.post(TICKETS, headers=CSRF, json=replay))
        assert (
            browser.post(
                TICKETS, headers=CSRF, json=ticket_form(newer, "暫存已到期")
            ).status_code
            == 410
        )

    # 確認最後一次嘗試也已被清除；原始及已驗證的重送都不依賴暫存。
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as restarted:
        login(restarted)
        assert (
            restarted.get(f"{ANSWERS}/{latest.json()['attempt_id']}").status_code == 404
        )
        assert restarted.get(f"{TICKETS}/{ticket['id']}").json() == ticket
        assert restarted.post(TICKETS, headers=CSRF, json=form).json() == ticket
        later_replays.append(restarted.post(TICKETS, headers=CSRF, json=replay))
        assert [result.status_code for result in later_replays] == [200, 200, 200]
        assert all(result.json() == ticket for result in later_replays)

        # 即使已保留重送識別，其他帳號也不能使用它取得快照。
        login(restarted, SUPPORT_FORM)
        assert restarted.post(TICKETS, headers=CSRF, json=replay).status_code == 404


# 建立超過一頁的實際工單，驗證同時間時以單號穩定排序及精確的末頁邊界。
def test_ticket_pages_have_stable_membership_and_strict_limits(
    answer_ready: Settings, provider_script: ProviderScript, clock: list[datetime]
) -> None:
    # 依正常配額只替前 20 次外呼提供結果，其後問答仍可建單。
    provider_script.responses.extend([model_output(None, []) for _ in range(20)])
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        expected = []
        for index in range(101):
            response = browser.post(
                TICKETS, headers=CSRF, json=ticket_form(ask(browser), f"描述 {index}")
            )
            assert response.status_code == 200
            ticket = response.json()
            summary = {key: ticket[key] for key in TICKET_SUMMARY_FIELDS}
            assert datetime.fromisoformat(summary["created_at"]) == clock[0]
            expected.append(summary)

        # 同一建立時間會以單號降序排序，既不重複也不漏掉第一百筆。
        expected.sort(key=itemgetter("id"), reverse=True)
        assert browser.get(TICKETS).json() == {
            "tickets": expected[:100],
            "has_more": True,
        }
        assert browser.get(f"{TICKETS}?limit=100").json() == browser.get(TICKETS).json()
        assert browser.get(f"{TICKETS}?limit=2&offset=99").json() == {
            "tickets": expected[99:],
            "has_more": False,
        }
        assert browser.get(f"{TICKETS}?offset=101").json() == {
            "tickets": [],
            "has_more": False,
        }
        for query in (
            "limit=0",
            "limit=101",
            "limit=1&limit=1",
            "limit=1.0",
            "limit=",
            "offset=-1",
            "offset=2147483648",
            "offset=0&offset=1",
        ):
            denied = browser.get(f"{TICKETS}?{query}")
            assert denied.status_code == 422
            assert denied.json() == {"detail": "工單分頁參數格式不正確。"}


# 進行中建單若在寫入時登入到期，停止嘗試和工單必須一起回滾。
def test_running_ticket_rolls_back_and_revokes_session_expired_during_insert(
    answer_ready: Settings, provider_script: ProviderScript, clock: list[datetime]
) -> None:
    # 保持供應商回覆未送達；晚到的工具呼叫不能替已到期的登入發起外呼。
    waiting, release = Event(), Event()

    # 透過現有的網路替身等候測試解除阻塞，不阻塞服務的事件迴圈。
    async def pending() -> httpx.Response:
        waiting.set()
        assert await anyio.to_thread.run_sync(release.wait, 5)
        return interaction("requires_action", CALL)

    provider_script.responses.append(pending)
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        with ThreadPoolExecutor(max_workers=2) as executor:
            answer = executor.submit(ask, browser)
            try:
                # 問答經 HTTP 受理；模型等待中從隔離資料庫取得本次真實識別。
                assert waiting.wait(5)
                with answer_ready.connect() as conn:
                    row = conn.execute(
                        "SELECT question_id, id, status FROM answer_attempts"
                    ).fetchone()
                assert row is not None and row[2] == "running"
                form = {
                    "question_id": str(row[0]),
                    "attempt_id": str(row[1]),
                    "description": "私人描述",
                }
                set_session_near_absolute_expiry(answer_ready, clock[0])

                # SHARE 允許查詢但阻塞 INSERT，此時停止更新已在同一交易內執行。
                with answer_ready.connect() as blocker:
                    blocker.execute("LOCK TABLE tickets IN SHARE MODE")
                    response = executor.submit(
                        browser.post, TICKETS, headers=CSRF, json=form
                    )
                    wait_for_search_lock(answer_ready, blocker.info.backend_pid)
                    clock[0] += timedelta(microseconds=1)
                denied = response.result(timeout=5)
                assert denied.status_code == 401
                assert "私人描述" not in denied.text

                # 模型仍在等待，另一連線只能看到原執行仍為 running、無工單且登入已撤銷。
                with answer_ready.connect() as conn:
                    assert conn.execute(
                        "SELECT status FROM answer_attempts WHERE id = %s",
                        (form["attempt_id"],),
                    ).fetchone() == ("running",)
                    assert conn.execute("SELECT count(*) FROM tickets").fetchone() == (
                        0,
                    )
                    assert conn.execute(
                        "SELECT count(*) FROM login_sessions"
                    ).fetchone() == (0,)
            finally:
                # 即使測試失敗也解除網路替身的阻塞，避免背景工作殘留。
                release.set()
            assert answer.result(timeout=5).status_code == 401

        # 舊問答依登入失效正常停止；只計入已送出的請求，重新登入後仍可正常建單。
        assert len(provider_script.requests) == 1
        with answer_ready.connect() as conn:
            assert conn.execute(
                "SELECT status, answer FROM answer_attempts"
            ).fetchone() == (
                "stopped",
                None,
            )
            assert conn.execute(
                "SELECT delivery FROM model_requests WHERE username = 'user'"
            ).fetchall() == [("sent",)]
        login(browser)
        assert browser.get(TICKETS).json() == {"tickets": [], "has_more": False}
        assert browser.post(TICKETS, headers=CSRF, json=form).status_code == 200


# 已提交的搜尋即使來不及交回模型，也會出現在逾時問答和工單中。
def test_ticket_preserves_tool_evidence_when_worker_expires_before_delivery(
    answer_ready: Settings,
    provider_script: ProviderScript,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 搜尋交易照常執行；只在交回工具結果前模擬時間耗盡。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    provider_script.responses.append(interaction("requires_action", CALL))

    # 不讓事件迴圈看見回傳值，驗證最終證據必須來自資料庫。
    def expires(
        settings: Settings, digest: bytes, attempt: Attempt, query: str, deadline: float
    ) -> list[knowledge.SearchResult]:
        assert search_attempt(settings, digest, attempt, query, deadline)
        raise AttemptDeadline

    monkeypatch.setattr(answers, "search_attempt", expires)
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        timed_out = ask(browser)
        form = ticket_form(timed_out, "等待已逾時")
        assert timed_out.json()["status"] == "timed_out"
        assert timed_out.json()["search_results"] == ORIGINAL_RESULTS["results"]
        created = browser.post(TICKETS, headers=CSRF, json=form)
        assert created.status_code == 200
        assert created.json()["answer_status"] == "timed_out"
        assert created.json()["search_results"] == ORIGINAL_RESULTS["results"]
    assert len(provider_script.requests) == 1


# 成功串流的終態沿用原 DTO；尚未受理的拒絕維持原本 HTTP JSON 狀態。
def test_answer_stream_uses_existing_terminal_result_and_pre_admission_errors(
    answer_ready: Settings, provider_script: ProviderScript
) -> None:
    # 在實際 FastAPI 入口要求 NDJSON，並驗證媒體類型與不可快取設定。
    provider_script.responses.append(model_output(None, []))
    headers = {**CSRF, "Accept": "application/x-ndjson"}
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        login(browser)
        response = browser.post(ANSWERS, headers=headers, json={"question": QUESTION})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-ndjson"
        assert response.headers["cache-control"] == "no-store"
        accepted, terminal = map(json.loads, response.text.splitlines())
        assert accepted["type"] == "accepted" and terminal["type"] == "result"
        assert terminal["result"]["status"] == "insufficient_evidence"
        for key in ("conversation_id", "question_id", "attempt_id"):
            assert terminal["result"][key] == accepted[key]
        assert (
            browser.get(f"{ANSWERS}/{accepted['attempt_id']}").json()
            == terminal["result"]
        )

        # 合法格式但不存在的嘗試不會開始串流或派發模型請求。
        rejected = browser.post(
            ANSWERS,
            headers=headers,
            json={**retry_form(accepted), "attempt_id": str(uuid4())},
        )
        assert rejected.status_code == 404
        assert rejected.json() == {"detail": "找不到這次問答。"}
        assert rejected.headers["content-type"] == "application/json"
    assert len(provider_script.requests) == 1


@pytest.mark.parametrize("disconnect_after_ticket", [False, True])
# 實際接收串流受理事件，在供應商第二次回應前建單，再測試晚到或客戶端中斷。
def test_ticket_stops_running_answer_with_committed_tool_evidence(
    answer_ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    provider_script: ProviderScript,
    disconnect_after_ticket: bool,
) -> None:
    # 讓舊嘗試有已完成的搜尋，並將後續供應商回應暫停在網路邊界。
    assert import_batch(monkeypatch, ORIGINAL) == 0
    with TestClient(create_app(answer_ready), base_url=ORIGIN) as browser:
        cookie = login(browser)

        # TestClient 會緩衝串流；直接使用完整 ASGI 入口測量事件及回應的先後。
        async def exercise() -> None:
            accepted, waiting, release = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )
            disconnected, settled = asyncio.Event(), asyncio.Event()
            frames: list[dict[str, str]] = []
            delivered = False

            # 舊模型晚到的工具呼叫若未受停止防護，會發出不應出現的外呼。
            async def pending() -> httpx.Response:
                waiting.set()
                try:
                    await release.wait()
                finally:
                    settled.set()
                return interaction("requires_action", {**CALL, "id": "late"})

            provider_script.responses.extend(
                [interaction("requires_action", CALL), pending]
            )
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
                    (b"accept", b"application/x-ndjson"),
                ]
                + [
                    (name.lower().encode("ascii"), value.encode("ascii"))
                    for name, value in {**CSRF, **replay_cookie(cookie)}.items()
                ],
            }

            # 送出一次本文，並讓監看端可以等待真實的連線結束。
            async def receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": json.dumps({"question": QUESTION}).encode("utf-8"),
                    }
                await disconnected.wait()
                return {"type": "http.disconnect"}

            # 只有送到 ASGI 外層的受理資料才視為客戶端已取得識別。
            async def send(message: Message) -> None:
                if body := message.get("body"):
                    for line in body.splitlines():
                        frame = json.loads(line)
                        frames.append(frame)
                        if frame.get("type") == "accepted":
                            accepted.set()

            # 以協程執行完整 ASGI 應用程式，讓 TaskGroup 負責等待清理。
            async def run_stream() -> None:
                await browser.app(scope, receive, send)

            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(run_stream)
                    await waiting.wait()
                    assert accepted.is_set(), "供應商等待時尚未送出受理事件"
                    admission = frames[0]
                    assert set(admission) == {
                        "type",
                        "conversation_id",
                        "question_id",
                        "attempt_id",
                    }
                    form = {
                        key: admission[key] for key in ("question_id", "attempt_id")
                    }

                    # 第二個 HTTP 操作只使用受理識別，工單應含先前已提交的來源。
                    ticket_response = browser.post(
                        TICKETS, headers=CSRF, json={**form, "description": "正在等待"}
                    )
                    assert ticket_response.status_code == 200
                    ticket = ticket_response.json()
                    assert ticket["answer_status"] == "stopped"
                    assert ticket["answer"] is None and ticket["citations"] == []
                    assert ticket["search_results"] == ORIGINAL_RESULTS["results"]

                    # 新嘗試可立即執行，舊嘗試不應占住對話或再呼叫模型。
                    provider_script.responses.append(model_output(None, []))
                    retried = browser.post(
                        ANSWERS, headers=CSRF, json=retry_form(admission)
                    )
                    assert retried.status_code == 200
                    assert retried.json()["attempt_id"] != admission["attempt_id"]
                    if disconnect_after_ticket:
                        disconnected.set()
                    else:
                        release.set()

            # 串流只回報安全錯誤，工單讀回與已結算的請求數量保持不變。
            assert settled.is_set()
            if disconnect_after_ticket:
                assert not release.is_set() and len(frames) == 1
            else:
                assert len(frames) == 2
                assert frames[1] == {
                    "type": "error",
                    "status": 404,
                    "detail": "找不到這次問答。",
                }
            assert browser.get(f"{TICKETS}/{ticket['id']}").json() == ticket
            assert len(provider_script.requests) == 3

        asyncio.run(exercise())

    # 兩個舊外呼與新外呼都已送出，停止等待不能釋放額度。
    with answer_ready.connect() as conn:
        assert (
            conn.execute(
                "SELECT delivery FROM model_requests WHERE username = 'user'"
            ).fetchall()
            == [("sent",)] * 3
        )
