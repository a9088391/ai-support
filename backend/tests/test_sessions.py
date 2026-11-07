import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from time import monotonic

import httpx
import pytest
from backend import cli, sessions
from backend.app import create_app
from backend.credentials import set_key, write_private
from backend.database import Settings
from backend.tests.conftest import SUPPORT_PASSWORD, USER_PASSWORD
from backend.tests.test_activation import success
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

ORIGIN = "http://localhost:8020"
SESSION_CSRF = {"X-CSRF-Protection": "1"}
CSRF = {"Origin": ORIGIN, **SESSION_CSRF}
COOKIE_NAME = "ai_support_session"
USER_FORM = {"username": "user", "password": USER_PASSWORD}
SUPPORT_FORM = {"username": "support", "password": SUPPORT_PASSWORD}
USER_IDENTITY = {"username": "user", "role": "user"}
SUPPORT_IDENTITY = {"username": "support", "role": "support"}
FIXTURE_KEY = "fixture-session-key"
ProviderFixture = tuple[list[httpx.Request], list[Callable[[], httpx.Response]]]


@pytest.fixture
# 明確執行一次啟用驗證，後續 HTTP、啟動與重啟都不能新增模型請求。
def ready(settings: Settings, mocked_provider: ProviderFixture) -> Iterator[Settings]:
    requests, responses = mocked_provider
    set_key(settings.key_file, FIXTURE_KEY)
    responses.append(success)
    assert cli.main(["verify-key"]) == 0
    yield settings
    assert len(requests) == 1


@pytest.fixture
# 以本機服務的同源埠與可辨識的 ASGI 連線來源測試 HTTP。
def client(ready: Settings) -> Iterator[TestClient]:
    with TestClient(
        create_app(ready), base_url=ORIGIN, client=("192.0.2.1", 50000)
    ) as browser:
        yield browser


@pytest.fixture
# 固定應用時鐘，讓實際 PostgreSQL 時間欄位能精確驗證邊界，無需等待。
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    current = [datetime(2026, 11, 7, 12, tzinfo=UTC)]

    # 每次操作取得測試當前指定的唯一時間點。
    def current_time() -> datetime:
        return current[0]

    monkeypatch.setattr(sessions, "utc_now", current_time)
    return current


# 從測試提供的原始帳號名稱算出預期的持久化計數桶。
def account_bucket(username: str) -> str:
    return hashlib.sha256(username.encode("ascii")).hexdigest()


# 建立明確的重送標頭，使請求使用已保存的舊憑證而非瀏覽器目前的 Cookie。
def replay_cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={token}"}


# 同一瀏覽器依序登入兩個角色，核對新舊工作階段、重啟與登出。
def test_login_rotation_restart_and_logout(client: TestClient, ready: Settings) -> None:
    # 帳號正規化不改角色；回應不暴露密碼、憑證或除身分外的欄位。
    first = client.post(
        "/api/login", headers=CSRF, json={**USER_FORM, "username": " USER "}
    )
    assert first.status_code == 200
    assert first.json() == USER_IDENTITY
    assert first.headers["cache-control"] == "no-store"
    first_cookie = first.headers["set-cookie"]
    first_token = first.cookies[COOKIE_NAME]
    assert len(first_token) == 43
    assert USER_PASSWORD not in first.text and first_token not in first.text
    assert {"HttpOnly", "Max-Age=28800", "Path=/", "SameSite=lax"} <= set(
        first_cookie.split("; ")[1:]
    )
    assert "Secure" not in first_cookie and "Domain=" not in first_cookie

    # 換角色成功時撤銷原憑證；後端儲存新憑證摘要及不同的 Argon2id 鹽值。
    second = client.post("/api/login", headers=CSRF, json=SUPPORT_FORM)
    assert second.status_code == 200
    assert second.json() == SUPPORT_IDENTITY
    second_token = second.cookies[COOKIE_NAME]
    assert second_token != first_token
    assert (
        client.get(
            "/api/session", headers={**SESSION_CSRF, **replay_cookie(first_token)}
        ).status_code
        == 401
    )
    assert client.get("/api/session", headers=SESSION_CSRF).json() == SUPPORT_IDENTITY
    with ready.connect() as conn:
        assert conn.execute(
            "SELECT token_digest, username FROM login_sessions"
        ).fetchall() == [(hashlib.sha256(second_token.encode()).digest(), "support")]
        accounts = conn.execute(
            "SELECT username, password_hash FROM demo_accounts ORDER BY username"
        ).fetchall()
    hashes = [row[1] for row in accounts if isinstance(row[1], str)]
    assert len(hashes) == 2
    assert all(
        encoded.startswith("$argon2id$v=19$m=19456,t=2,p=1$") for encoded in hashes
    )
    assert hashes[0].split("$")[4] != hashes[1].split("$")[4]

    # 新服務沿用資料與驗證結果，既有 Cookie 能刷新，登出後立即拒絕重送。
    with TestClient(create_app(ready), base_url=ORIGIN) as restarted:
        cookie = replay_cookie(second_token)
        session = restarted.get("/api/session", headers={**SESSION_CSRF, **cookie})
        assert session.json() == SUPPORT_IDENTITY
        assert session.headers["cache-control"] == "no-store"
        for _ in range(2):
            logged_out = restarted.post("/api/logout", headers={**CSRF, **cookie})
            assert logged_out.status_code == 204 and logged_out.content == b""
            assert logged_out.headers["cache-control"] == "no-store"
            assert "Max-Age=0" in logged_out.headers["set-cookie"]
        assert (
            restarted.get(
                "/api/session", headers={**SESSION_CSRF, **cookie}
            ).status_code
            == 401
        )

    # 重啟不得重新產生密碼雜湊、覆寫額度或保留已撤銷的工作階段。
    with ready.connect() as conn:
        assert (
            conn.execute(
                "SELECT username, password_hash FROM demo_accounts ORDER BY username"
            ).fetchall()
            == accounts
        )
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM login_sessions").fetchone() == (0,)


# 建立明確的憑證無效回覆，讓真實 CLI 流程更新啟用紀錄。
def invalid_key_response() -> httpx.Response:
    return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})


# 從未設定、未驗證、無效走到就緒，再確認失去啟用仍能撤銷舊憑證。
def test_activation_gate_and_logout_without_key(
    settings: Settings, mocked_provider: ProviderFixture
) -> None:
    requests, responses = mocked_provider
    with TestClient(create_app(settings), base_url=ORIGIN) as browser:
        # 三個未就緒狀態皆拒絕登入及工作階段，且健康檢查仍可用。
        for state in ("unconfigured", "unverified", "invalid"):
            if state == "unverified":
                set_key(settings.key_file, FIXTURE_KEY)
            elif state == "invalid":
                responses.append(invalid_key_response)
                assert cli.main(["verify-key"]) == 1
            assert browser.get("/api/setup").json()["status"] == state
            assert browser.get("/health").status_code == 200
            login = browser.post("/api/login", headers=CSRF, json=USER_FORM)
            session = browser.get("/api/session", headers=SESSION_CSRF)
            assert login.status_code == session.status_code == 503
            assert (
                login.headers["cache-control"]
                == session.headers["cache-control"]
                == "no-store"
            )
            assert "set-cookie" not in login.headers

        # 唯有明確驗證才可登入；Key 移除後的登出仍須完成資料庫撤銷。
        responses.append(success)
        assert cli.main(["verify-key"]) == 0
        login = browser.post("/api/login", headers=CSRF, json=USER_FORM)
        assert login.status_code == 200
        cookie = replay_cookie(login.cookies[COOKIE_NAME])
        settings.key_file.unlink()
        assert (
            browser.get("/api/session", headers={**SESSION_CSRF, **cookie}).status_code
            == 503
        )
        assert (
            browser.post("/api/logout", headers={**CSRF, **cookie}).status_code == 204
        )

        # 恢復並重驗同一虛構 Key，舊憑證仍失效且沒有隱含外呼。
        set_key(settings.key_file, FIXTURE_KEY)
        responses.append(success)
        assert cli.main(["verify-key"]) == 0
        assert (
            browser.get("/api/session", headers={**SESSION_CSRF, **cookie}).status_code
            == 401
        )
        assert len(requests) == 3


# 欄位、字串編碼及 JSON 格式錯誤皆使用同一安全訊息，不改動登入狀態。
def test_bad_credentials_and_unsafe_input(client: TestClient, ready: Settings) -> None:
    # 未知帳號即使使用有效密碼，與現有帳號密碼錯誤也須得到相同失敗。
    wrong = client.post(
        "/api/login", headers=CSRF, json={**USER_FORM, "password": "wrong"}
    )
    unknown = client.post(
        "/api/login", headers=CSRF, json={**USER_FORM, "username": "other"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert "set-cookie" not in wrong.headers and "set-cookie" not in unknown.headers

    # 區分欄位型別、字元長度、未知欄位和原始位元組解析失敗。
    content_headers = {**CSRF, "Content-Type": "application/json"}
    nested = b"[" * 15000 + b"0" + b"]" * 15000
    surrogate = rb'{"username":"user","password":"\uD800"}'
    assert b"\\uD800" in surrogate and len(nested) < 64 * 1024
    for raw in (
        surrogate,
        nested,
        b'{"username":"user","password":"fixture-do-not-echo"',
        b'{"username":1,"password":"fixture-do-not-echo"}',
        b'{"username":"user","password":"fixture-do-not-echo","fixture-do-not-echo":1}',
        b'{"username":"' + b"x" * 65 + b'","password":"short"}',
        b'{"username":"user","password":"' + b"x" * 129 + b'"}',
        b'{"username":"   ","password":"fixture-do-not-echo"}',
        b'{"username":"user","password":""}',
    ):
        rejected = client.post("/api/login", headers=content_headers, content=raw)
        assert rejected.status_code == 422
        assert rejected.json() == {"detail": "登入欄位格式不正確。"}
        assert rejected.headers["cache-control"] == "no-store"
        assert "set-cookie" not in rejected.headers
    assert client.post("/api/login", headers=CSRF, content=b"{}").status_code == 422

    # 只有兩次合法形狀的錯誤登入取得額度，其餘在驗證層停止。
    with ready.connect() as conn:
        assert conn.execute(
            "SELECT sum(attempts) FROM login_rate_limits WHERE dimension = 'source'"
        ).fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM login_sessions").fetchone() == (0,)


# 對異動拒絕缺失、重複、畸形及跨來源標頭，且不信任轉送資訊。
def test_same_origin_and_csrf_precede_api_effects(
    client: TestClient, ready: Settings
) -> None:
    # 重複標頭以清單保留原始形狀；每一組皆嘗試提交正確密碼。
    for headers in (
        [],
        [("Origin", ORIGIN)],
        [("X-CSRF-Protection", "1")],
        [("Origin", ORIGIN), ("X-CSRF-Protection", "0")],
        [*CSRF.items(), ("X-CSRF-Protection", "1")],
        [*CSRF.items(), ("Origin", ORIGIN)],
        [("Origin", "null"), ("X-CSRF-Protection", "1")],
        [("Origin", f"{ORIGIN}/"), ("X-CSRF-Protection", "1")],
        [("Origin", "http://localhost:8021"), ("X-CSRF-Protection", "1")],
        [("Origin", "http://127.0.0.1:8020"), ("X-CSRF-Protection", "1")],
        [
            ("Origin", "https://localhost:8020"),
            ("X-CSRF-Protection", "1"),
            ("X-Forwarded-Proto", "https"),
            ("X-Forwarded-Host", "localhost:8020"),
        ],
    ):
        rejected = client.post("/api/login", headers=headers, json=USER_FORM)
        assert rejected.status_code == 403
        assert rejected.headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in rejected.headers

    # 主機只允許既有 loopback 名稱且須唯一；其他異動 API 也受相同檢查。
    for host_headers in (
        [("Host", "localhost:65536")],
        [("Host", "localhost:8020"), ("Host", "localhost:8020")],
    ):
        response = client.post(
            "/api/login", headers=[*CSRF.items(), *host_headers], json=USER_FORM
        )
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"
    assert client.patch("/api/questions").status_code == 403
    with ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM login_rate_limits").fetchone() == (0,)

    # 無效的登出不撤銷現有工作階段；同源且自訂標頭正確才可撤銷。
    assert client.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
    assert client.post("/api/logout", headers={"Origin": ORIGIN}).status_code == 403
    assert client.get("/api/session", headers=SESSION_CSRF).json() == USER_IDENTITY
    assert client.post("/api/logout", headers=CSRF).status_code == 204


# 以已登入 Cookie 查詢，驗證只有通過來源防護的請求可更新閒置時間。
def test_session_refresh_requires_csrf(
    client: TestClient, ready: Settings, clock: list[datetime]
) -> None:
    # 透過公開登入建立工作階段，再推進固定時鐘使意外刷新可見。
    started = clock[0]
    login = client.post("/api/login", headers=CSRF, json=USER_FORM)
    assert login.status_code == 200
    cookie = replay_cookie(login.cookies[COOKIE_NAME])
    clock[0] += timedelta(minutes=10)

    # 保留原始重複標頭，逐次核對拒絕回覆與資料庫中的活動時間。
    for headers in (
        [],
        [("Origin", ORIGIN)],
        [("X-CSRF-Protection", "0")],
        [*SESSION_CSRF.items(), *SESSION_CSRF.items()],
        [*SESSION_CSRF.items(), ("Origin", "http://localhost:8021")],
        [*SESSION_CSRF.items(), ("Origin", "null")],
        [*CSRF.items(), ("Origin", ORIGIN)],
    ):
        response = client.get("/api/session", headers=[*cookie.items(), *headers])
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
        with ready.connect() as conn:
            assert conn.execute(
                "SELECT last_seen_at FROM login_sessions"
            ).fetchone() == (started,)

    # 無法辨識或重複的刷新參數不得觸發刷新，也不回顯查詢值。
    for params in (
        [("refresh", "fixture-invalid-value")],
        [("refresh", "false"), ("refresh", "true")],
    ):
        response = client.get(
            "/api/session", headers={**SESSION_CSRF, **cookie}, params=params
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "工作階段查詢參數格式不正確。"}
    with ready.connect() as conn:
        assert conn.execute("SELECT last_seen_at FROM login_sessions").fetchone() == (
            started,
        )

    # 跨來源腳本不能經由預檢獲准送出自訂標頭。
    preflight = client.options(
        "/api/session",
        headers={
            "Origin": "http://localhost:8021",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-csrf-protection",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-headers" not in preflight.headers

    # 同源 GET 可只帶自訂標頭，也可帶上正確的 Origin，兩者都會刷新。
    for valid_headers in (SESSION_CSRF, CSRF):
        clock[0] += timedelta(minutes=1)
        response = client.get("/api/session", headers={**valid_headers, **cookie})
        assert response.status_code == 200
        assert response.json() == USER_IDENTITY
        assert response.headers["cache-control"] == "no-store"
        with ready.connect() as conn:
            assert conn.execute(
                "SELECT last_seen_at FROM login_sessions"
            ).fetchone() == (clock[0],)

    # 刷新後的閒置期限仍在 30 分鐘等值失效。
    clock[0] += timedelta(minutes=30)
    assert (
        client.get("/api/session", headers={**SESSION_CSRF, **cookie}).status_code
        == 401
    )


@pytest.mark.parametrize("expiry", ["idle", "absolute"])
@pytest.mark.parametrize("refresh", [True, False], ids=["refresh", "passive"])
# 透過 HTTP 驗證主動刷新與被動查詢的期限邊界及資料庫活動時間。
def test_session_expiry_boundary(
    client: TestClient,
    ready: Settings,
    clock: list[datetime],
    expiry: str,
    refresh: bool,
) -> None:
    # 登入建立初始時間，累計期限案例以資料庫夾具模擬先前活動。
    started = clock[0]
    login = client.post("/api/login", headers=CSRF, json=USER_FORM)
    assert login.status_code == 200
    last_seen = started
    if expiry == "absolute":
        boundary = started + timedelta(hours=8)
        last_seen = boundary - timedelta(minutes=1)
        with ready.connect() as conn:
            conn.execute(
                "UPDATE login_sessions SET last_seen_at = %s",
                (last_seen,),
            )
    else:
        boundary = started + timedelta(minutes=30)

    # 到期前一微秒都可讀取身分，但只有主動刷新會延後閒置期限。
    before = boundary - timedelta(microseconds=1)
    clock[0] = before
    params = {"refresh": "true" if refresh else "false"}
    response = client.get("/api/session", headers=SESSION_CSRF, params=params)
    assert response.status_code == 200
    assert response.json() == USER_IDENTITY
    assert "set-cookie" not in response.headers
    with ready.connect() as conn:
        assert conn.execute(
            "SELECT created_at, last_seen_at FROM login_sessions"
        ).fetchone() == (started, before if refresh else last_seen)
    if expiry == "idle" and refresh:
        boundary = before + timedelta(minutes=30)

    # 等於上限即撤銷，之後重送與時鐘回撥都不能恢復。
    for now in (boundary, boundary + timedelta(microseconds=1), before):
        clock[0] = now
        assert (
            client.get("/api/session", headers=SESSION_CSRF, params=params).status_code
            == 401
        )
    with ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM login_sessions").fetchone() == (0,)


# 帳號跨來源及重啟共用固定時間窗，來源與帳號的交易在回覆前已提交。
def test_account_limit_survives_restart_and_normalizes_username(
    client: TestClient, ready: Settings, clock: list[datetime]
) -> None:
    # 五次合法形狀的錯誤嘗試都計入原始帳號。
    wrong = {**USER_FORM, "password": "wrong"}
    for _ in range(5):
        assert client.post("/api/login", headers=CSRF, json=wrong).status_code == 401

    # 以不同來源重新啟動服務，大小寫與空白無法繞過帳號上限。
    with TestClient(
        create_app(ready), base_url=ORIGIN, client=("192.0.2.2", 50000)
    ) as restarted:
        limited = restarted.post(
            "/api/login", headers=CSRF, json={**USER_FORM, "username": " USER "}
        )
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "900"
        assert limited.headers["cache-control"] == "no-store"
        with ready.connect() as conn:
            assert conn.execute(
                "SELECT dimension, bucket, attempts FROM login_rate_limits ORDER BY dimension, bucket"
            ).fetchall() == [
                ("account", account_bucket("user"), 5),
                ("source", "192.0.2.1", 5),
                ("source", "192.0.2.2", 1),
            ]

        # 時間窗結束前仍有限制，等於到期即清除並接受新嘗試。
        started = clock[0]
        clock[0] = started + timedelta(minutes=15, microseconds=-1)
        waiting = restarted.post("/api/login", headers=CSRF, json=USER_FORM)
        assert waiting.status_code == 429 and waiting.headers["retry-after"] == "1"
        clock[0] = started + timedelta(minutes=15)
        assert (
            restarted.post("/api/login", headers=CSRF, json=USER_FORM).json()
            == USER_IDENTITY
        )
    with ready.connect() as conn:
        assert conn.execute(
            "SELECT dimension, bucket, attempts FROM login_rate_limits ORDER BY dimension, bucket"
        ).fetchall() == [
            ("account", account_bucket("user"), 1),
            ("source", "192.0.2.2", 1),
        ]


# 來源跨帳號共用限額，偽造代理標頭與改用任意帳號皆無法避開。
def test_source_limit_uses_connection_peer(client: TestClient, ready: Settings) -> None:
    # 以一個帳號反覆嘗試填滿同一來源，改寫轉送標頭不能建立新來源。
    for index in range(20):
        response = client.post(
            "/api/login",
            headers={
                **CSRF,
                "X-Forwarded-For": f"203.0.113.{index + 1}",
                "Forwarded": f"for=198.51.100.{index + 1}",
            },
            json={**USER_FORM, "password": "wrong"},
        )
        assert response.status_code == (401 if index < 5 else 429)

    # 來源受限時連尚未嘗試的客服也被阻擋；另一個真實來源仍可登入。
    assert client.post("/api/login", headers=CSRF, json=SUPPORT_FORM).status_code == 429
    with ready.connect() as conn:
        assert conn.execute(
            "SELECT dimension, bucket, attempts FROM login_rate_limits ORDER BY dimension, bucket"
        ).fetchall() == [
            ("account", account_bucket("user"), 5),
            ("source", "192.0.2.1", 20),
        ]
    with TestClient(
        create_app(ready), base_url=ORIGIN, client=("192.0.2.2", 50000)
    ) as other:
        assert (
            other.post("/api/login", headers=CSRF, json=SUPPORT_FORM).json()
            == SUPPORT_IDENTITY
        )


# 探測其他不存在的帳號不會提前限制新名稱，單一未知帳號仍獨立限流。
def test_unknown_account_has_no_shared_enumeration_bucket(client: TestClient) -> None:
    # 用不同名稱累積錯誤，避免把任一不存在的帳號判成已受帳號限制。
    for index in range(6):
        response = client.post(
            "/api/login",
            headers=CSRF,
            json={"username": f"missing-{index}", "password": "wrong"},
        )
        assert response.status_code == 401

    # 同一名稱的不同大小寫共用額度；新名稱仍回傳相同的普通拒絕。
    for _ in range(4):
        result = client.post(
            "/api/login",
            headers=CSRF,
            json={"username": " MISSING-0 ", "password": "wrong"},
        )
        assert result.status_code == 401
    limited = client.post(
        "/api/login",
        headers=CSRF,
        json={"username": "missing-0", "password": "wrong"},
    )
    assert limited.status_code == 429
    fresh = client.post(
        "/api/login",
        headers=CSRF,
        json={"username": "missing-new", "password": "wrong"},
    )
    assert fresh.status_code == 401 and fresh.json() == result.json()


# 在同一來源及帳號發起競爭，核對 PostgreSQL 只放行五次雜湊嘗試。
def test_concurrent_account_limit_is_atomic(
    client: TestClient, ready: Settings
) -> None:
    simultaneous = Barrier(8)

    # 同步放行八個 HTTP 請求，服務須決定唯一可消耗的額度。
    def attempt(_: int) -> int:
        simultaneous.wait(timeout=8)
        response = client.post(
            "/api/login", headers=CSRF, json={**USER_FORM, "password": "wrong"}
        )
        return int(response.status_code)

    with ThreadPoolExecutor(8) as workers:
        assert sorted(workers.map(attempt, range(8))) == [401] * 5 + [429] * 3

    # 另開資料庫連線讀取已提交計數，驗證受帳號限制時來源仍有累計。
    with ready.connect() as conn:
        assert conn.execute(
            "SELECT dimension, attempts FROM login_rate_limits ORDER BY dimension"
        ).fetchall() == [("account", 5), ("source", 8)]


# 在同一來源剩一次額度時，以兩個帳號併發競爭；只准一個建立帳號計數。
def test_concurrent_source_limit_is_atomic(client: TestClient, ready: Settings) -> None:
    # 預留十九次來源嘗試，另兩個名稱都尚未提交。
    for index in range(19):
        response = client.post(
            "/api/login", headers=CSRF, json={**USER_FORM, "password": "wrong"}
        )
        assert response.status_code == (401 if index < 5 else 429)
    simultaneous = Barrier(2)

    # 不同帳號也須以同一來源限額交易排序。
    def attempt(username: str) -> int:
        simultaneous.wait(timeout=8)
        response = client.post(
            "/api/login", headers=CSRF, json={"username": username, "password": "wrong"}
        )
        return int(response.status_code)

    with ThreadPoolExecutor(2) as workers:
        assert sorted(workers.map(attempt, ("support", "missing"))) == [401, 429]

    # 被來源拒絕的請求不能建立帳號計數或超出來源上限。
    with ready.connect() as conn:
        assert conn.execute(
            """
            SELECT dimension, attempts FROM login_rate_limits
            WHERE bucket <> %s ORDER BY dimension
            """,
            (account_bucket("user"),),
        ).fetchall() == [("account", 1), ("source", 20)]


# 兩個已到期的 Cookie 同時換發，必須各自完成且只留下新的憑證。
def test_concurrent_rotation_of_expired_sessions(
    client: TestClient,
    ready: Settings,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 建立兩個獨立瀏覽器工作階段，避免第二次登入在準備時撤銷第一個。
    earlier: list[str] = []
    for form in (USER_FORM, SUPPORT_FORM):
        client.cookies.clear()
        response = client.post("/api/login", headers=CSRF, json=form)
        assert response.status_code == 200
        earlier.append(response.cookies[COOKIE_NAME])
    clock[0] += timedelta(minutes=30)
    simultaneous = Barrier(2)
    password_matches = sessions.password_matches

    # 限流交易先提交，再同步放行兩個通過雜湊的換發交易。
    def verify_together(password: str, encoded: str) -> bool:
        verified = password_matches(password, encoded)
        simultaneous.wait(timeout=8)
        return verified

    monkeypatch.setattr(sessions, "password_matches", verify_together)

    # 以各自保存的到期憑證呼叫公開 API，檢查兩個角色都成功換發。
    def rotate(original: tuple[dict[str, str], str]) -> str:
        form, token = original
        result = client.post(
            "/api/login", headers={**CSRF, **replay_cookie(token)}, json=form
        )
        assert result.status_code == 200
        assert result.json() == {"username": form["username"], "role": form["username"]}
        return str(result.cookies[COOKIE_NAME])

    with ThreadPoolExecutor(2) as workers:
        issued = list(
            workers.map(rotate, zip((USER_FORM, SUPPORT_FORM), earlier, strict=True))
        )

    # 兩個新憑證各自有效，且資料庫已刪除兩個到期的原始憑證。
    for token, identity in zip(issued, (USER_IDENTITY, SUPPORT_IDENTITY), strict=True):
        assert (
            client.get(
                "/api/session", headers={**SESSION_CSRF, **replay_cookie(token)}
            ).json()
            == identity
        )
    with ready.connect() as conn:
        stored = conn.execute("SELECT token_digest FROM login_sessions").fetchall()
        assert set(stored) == {
            (hashlib.sha256(token.encode()).digest(),) for token in issued
        }


# 以真正的列鎖重現刷新尚未提交時另一個請求登出，驗證撤銷後不能復活。
def test_logout_racing_refresh_removes_original_session(
    client: TestClient, ready: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = client.post("/api/login", headers=CSRF, json=USER_FORM)
    assert login.status_code == 200
    cookie = replay_cookie(login.cookies[COOKIE_NAME])
    refresh_locked = Event()
    allow_refresh = Event()

    # 時鐘只會在刷新取得列鎖後執行，據此讓登出確實等待同一筆資料。
    def locked_time() -> datetime:
        refresh_locked.set()
        assert allow_refresh.wait(timeout=8)
        return datetime.now(UTC)

    monkeypatch.setattr(sessions, "utc_now", locked_time)

    # 觀察資料庫的阻塞關係後再提交刷新，不依賴任意等待時間。
    with ThreadPoolExecutor(2) as workers:
        refreshing = workers.submit(
            client.get, "/api/session", headers={**SESSION_CSRF, **cookie}
        )
        try:
            assert refresh_locked.wait(timeout=8)
            logging_out = workers.submit(
                client.post, "/api/logout", headers={**CSRF, **cookie}
            )
            deadline = monotonic() + 5
            with ready.connect() as conn:
                conn.autocommit = True
                while True:
                    blocking = conn.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM pg_stat_activity
                            WHERE datname = current_database()
                              AND query LIKE 'DELETE FROM login_sessions WHERE token_digest = %'
                              AND cardinality(pg_blocking_pids(pid)) > 0
                        )
                        """
                    ).fetchone()
                    if blocking == (True,):
                        break
                    assert monotonic() < deadline
        finally:
            allow_refresh.set()

        # 刷新先提交、登出後撤銷，瀏覽器重送原始 Cookie 仍須被拒絕。
        assert refreshing.result(timeout=8).status_code == 200
        assert logging_out.result(timeout=8).status_code == 204
    assert (
        client.get("/api/session", headers={**SESSION_CSRF, **cookie}).status_code
        == 401
    )


# 遺失本機檔案後重新產生的值不可改寫先前雜湊或讓工作階段悄悄消失。
def test_provisioned_password_mismatch_fails_closed(
    client: TestClient, ready: Settings
) -> None:
    login = client.post("/api/login", headers=CSRF, json=USER_FORM)
    assert login.status_code == 200
    original = ready.demo_passwords_file.read_text()
    replacement = f"user={'C' * 43}\nsupport={SUPPORT_PASSWORD}\n"
    write_private(ready.demo_passwords_file, replacement, replace=True)

    # 以同一個資料庫重新啟動，應拒絕不符的檔案且原始內容不出現在錯誤。
    with pytest.raises(RuntimeError, match="密碼檔與資料庫不符") as caught:
        with TestClient(create_app(ready), base_url=ORIGIN):
            pytest.fail("不相符的示範帳號設定不應啟動。")
    assert "C" * 43 not in str(caught.value)
    assert ready.demo_passwords_file.read_text() == replacement

    # 復原受保護的原始檔後可重啟，舊 Cookie、登入與啟用仍有效。
    write_private(ready.demo_passwords_file, original, replace=True)
    with TestClient(create_app(ready), base_url=ORIGIN) as restarted:
        assert (
            restarted.get(
                "/api/session",
                headers={**SESSION_CSRF, **replay_cookie(login.cookies[COOKIE_NAME])},
            ).json()
            == USER_IDENTITY
        )
        assert (
            restarted.post("/api/login", headers=CSRF, json=USER_FORM).json()
            == USER_IDENTITY
        )


# 明確區分宣告長度與實際分段本文在 64 KiB 的接受／拒絕邊界。
def test_body_limit_with_declared_and_streamed_length(
    client: TestClient, ready: Settings
) -> None:
    # 合法 JSON 以空白填滿限制，成功登入可證明到達後續資料庫層。
    form = ('{"username":"user","password":"' + USER_PASSWORD + '"}').encode("ascii")
    exact = form.ljust(64 * 1024)
    oversized = exact + b" "
    headers = {**CSRF, "Content-Type": "application/json"}

    # Content-Length 由客戶端依原始位元組設定，超限不取得登入額度。
    assert (
        client.post("/api/login", headers=headers, content=exact).json()
        == USER_IDENTITY
    )
    response = client.post("/api/login", headers=headers, content=oversized)
    assert (
        response.status_code == 413 and response.headers["cache-control"] == "no-store"
    )
    malformed = client.post(
        "/api/login",
        headers={**headers, "Content-Length": "10"},
        content=form,
    )
    assert malformed.status_code == 400

    # ASGI 傳輸保留多次 receive，不以宣告的 Content-Length 代替實際累計。
    async def streamed() -> None:
        transport = httpx.ASGITransport(app=client.app, client=("192.0.2.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as browser:
            for raw, expected, chunked in (
                (exact, 200, True),
                (oversized, 413, True),
                (oversized, 413, False),
            ):
                # 將最後一個位元組分成單獨一段，覆蓋剛達與跨越上限。
                async def chunks() -> AsyncIterator[bytes]:
                    yield raw[:32768]
                    yield raw[32768:65536]
                    yield raw[65536:]

                request = browser.build_request(
                    "POST", "/api/login", headers=headers, content=chunks()
                )
                assert "content-length" not in request.headers
                assert request.headers["transfer-encoding"] == "chunked"
                if not chunked:
                    del request.headers["transfer-encoding"]
                streamed_response = await browser.send(request)
                assert streamed_response.status_code == expected
                assert streamed_response.headers["cache-control"] == "no-store"
                if expected == 200:
                    assert streamed_response.json() == USER_IDENTITY

    asyncio.run(streamed())

    # 恰好兩次合法本文取得來源額度；錯誤本文無資料庫副作用。
    with ready.connect() as conn:
        assert conn.execute(
            "SELECT attempts FROM login_rate_limits WHERE dimension = 'source'"
        ).fetchone() == (2,)


# 在合法登入本文尚未收完時中斷連線，完整憑證片段也不可觸發登入。
def test_disconnected_body_never_reaches_login(
    client: TestClient, ready: Settings
) -> None:
    # 使用原始 ASGI 接收事件，避免 HTTP 測試客戶端先收完整個請求本文。
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/login",
        "scheme": "http",
        "client": ("192.0.2.1", 50000),
        "headers": [
            (b"host", b"localhost:8020"),
            (b"origin", ORIGIN.encode("ascii")),
            (b"x-csrf-protection", b"1"),
            (b"content-type", b"application/json"),
        ],
    }
    events: list[Message] = [
        {
            "type": "http.request",
            "body": ('{"username":"user","password":"' + USER_PASSWORD + '"}').encode(
                "ascii"
            ),
            "more_body": True,
        },
        {"type": "http.disconnect"},
    ]
    sent: list[Message] = []

    # 如實交付本文片段與中斷通知，服務不應向已關閉的連線回覆。
    async def receive() -> Message:
        return events.pop(0)

    # 留下任何嘗試送出的事件，檢查中斷時確實沒有回覆。
    async def send(message: Message) -> None:
        sent.append(message)

    # 透過真正的應用程式入口交付事件，包含生產的安全中介層。
    async def disconnect() -> None:
        await client.app(scope, receive, send)

    asyncio.run(disconnect())
    assert sent == []

    # 未完成的請求不能消耗額度或建立登入狀態。
    with ready.connect() as conn:
        assert conn.execute("SELECT count(*) FROM login_rate_limits").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM login_sessions").fetchone() == (0,)
