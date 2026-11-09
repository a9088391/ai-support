import fcntl
import hashlib
import ssl
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import httpcore
import httpx
import psycopg
import pytest
from backend import activation, cli, credentials
from backend.app import create_app
from backend.credentials import GeminiCredential, read_key, set_key
from backend.database import Settings, initialize, reserve_request, settle_request
from backend.provider import MODEL
from backend.tests.test_provider import SUCCESS
from fastapi.testclient import TestClient


# 產生成功回應供啟用流程使用，格式沿用固定 SDK 的 HTTP 契約。
def success() -> httpx.Response:
    return httpx.Response(200, json=SUCCESS)


# 用公共入口同時驗證安全 DTO 與業務 API 啟用門檻。
def assert_setup(client: TestClient, state: str) -> None:
    # 查詢狀態不會送出模型請求，也不包含任何秘密欄位。
    response = client.get("/api/setup")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json().keys() == {"status", "message", "model"}
    assert response.json()["status"] == state
    assert response.json()["model"] == MODEL
    assert response.json()["message"]

    # 已就緒才可落入路由層的 404；未就緒時直接呼叫也會被拒絕。
    business = client.get("/api/questions")
    assert business.status_code == (404 if state == "ready" else 503)
    assert business.headers["content-type"] == "application/json"


# 以已完成啟用的公開 HTTP 入口區分服務可用與瀏覽器尚未登入。
def test_ready_session_requires_login(
    settings: Settings,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
) -> None:
    requests, responses = mocked_provider
    set_key(settings.key_file, "fixture-session-key")
    responses.append(success)
    assert cli.main(["verify-key"]) == 0

    # 通過來源防護但沒有登入憑證時，查詢須回傳未登入且不得外呼。
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        response = client.get("/api/session", headers={"X-CSRF-Protection": "1"})
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    assert len(requests) == 1


# 驗證未設定到就緒、重啟、換回同 Key 與供應商／模型不符的 API 和 CLI 行為。
def test_activation_lifecycle(
    settings: Settings,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests, responses = mocked_provider
    first_key = "fixture-first-secret"

    # 缺 Key 不影響健康檢查；任何 HTTP 方法都不能觸發驗證。
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert client.get("/health").status_code == 200
        assert_setup(client, "unconfigured")
        assert (
            client.post(
                "/api/setup",
                headers={"Origin": "http://localhost", "X-CSRF-Protection": "1"},
            ).status_code
            == 503
        )
        assert cli.main(["verify-key"]) == 1
        assert not requests

        # 新 Key 先拒絕 API，只有操作者執行 CLI 後才送出一次請求。
        set_key(settings.key_file, first_key)
        assert_setup(client, "unverified")
        responses.append(success)
        assert cli.main(["verify-key"]) == 0
        assert_setup(client, "ready")
        assert len(requests) == 1

        # 實際持久化檢查指紋、額度與送出狀態，且輸出不含 Key。
        with settings.connect() as conn:
            assert conn.execute(
                "SELECT fingerprint, provider, model, status FROM activation"
            ).fetchone() == (
                hashlib.sha256(first_key.encode()).hexdigest(),
                "google-gemini",
                MODEL,
                "ready",
            )
            assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (1,)
            assert conn.execute("SELECT delivery FROM model_requests").fetchone() == (
                "sent",
            )
        assert first_key not in client.get("/api/setup").text
        assert first_key not in caplog.text
        cli_output = capsys.readouterr()
        assert first_key not in cli_output.out + cli_output.err

    # 新應用執行個體會重跑初始化，並從 PostgreSQL 沿用同設定的成功紀錄。
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert_setup(client, "ready")
        assert len(requests) == 1
        with settings.connect() as conn:
            assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (1,)
            conn.execute("UPDATE activation SET model = 'other-model'")

        # 模型及供應商分別不吻合時都會拒絕 API。
        assert_setup(client, "unverified")
        with settings.connect() as conn:
            conn.execute(
                "UPDATE activation SET model = %s, provider = 'other-provider'",
                (MODEL,),
            )
        assert_setup(client, "unverified")
        with settings.connect() as conn:
            conn.execute("UPDATE activation SET provider = 'google-gemini'")

        # 換回曾使用過的 Key 也需重新驗證；設定操作本身沒有模型外呼。
        set_key(settings.key_file, "fixture-replacement-secret")
        assert_setup(client, "unverified")
        set_key(settings.key_file, first_key)
        assert_setup(client, "unverified")
        assert len(requests) == 1


@pytest.mark.parametrize("initially_ready", [False, True])
# 驗證暫時錯誤不誤判憑證，只有 Google 明確拒絕才變成 invalid。
def test_provider_errors(
    settings: Settings,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
    initially_ready: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests, responses = mocked_provider
    set_key(settings.key_file, "fixture-secret")

    # 用真實 CLI 及 SDK 分別取得受控的限流、逾時及憑證拒絕。
    def limited() -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "fixture-secret"}})

    # 模擬已開始送出但讀取超時，應保留用量及送達不明狀態。
    def timeout() -> httpx.Response:
        raise httpx.ReadTimeout("fixture timeout")

    # 用 Google 結構化錯誤回覆標記憑證拒絕。
    def rejected() -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "details": [
                        {"reason": "API_KEY_INVALID", "domain": "googleapis.com"}
                    ]
                }
            },
        )

    # 在未驗證與已就緒兩種基準狀態下，檢查錯誤後的 API 門檻。
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        if initially_ready:
            responses.append(success)
            assert cli.main(["verify-key"]) == 0
        expected = "ready" if initially_ready else "unverified"

        capsys.readouterr()
        responses.append(limited)
        assert cli.main(["verify-key"]) == 1
        message = capsys.readouterr().err
        assert "429" in message and "fixture-secret" not in message
        assert_setup(client, expected)

        responses.append(timeout)
        assert cli.main(["verify-key"]) == 1
        assert_setup(client, expected)

        responses.append(rejected)
        assert cli.main(["verify-key"]) == 1
        assert_setup(client, "invalid")

    # 限流及憑證拒絕已送出；逾時另列不明，每筆皆保留原額度。
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (
            len(requests),
        )
        assert conn.execute(
            "SELECT count(*) FROM model_requests WHERE delivery = 'unknown'"
        ).fetchone() == (1,)


# 設定旋轉期間，遲到的舊憑證結果只能結算用量，不得蓋掉新憑證的成功。
def test_late_probe_cannot_overwrite_new_config(
    settings: Settings,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
) -> None:
    requests, responses = mocked_provider
    set_key(settings.key_file, "fixture-old")
    entered = Event()
    release = Event()

    # 在資料庫預留已提交、網路呼叫進入傳輸層後，延後舊請求回覆。
    def slow() -> httpx.Response:
        entered.set()
        assert release.wait(timeout=8)
        return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})

    # 保持第一個請求在途時啟動新設定的驗證。
    responses.extend((slow, success))
    with ThreadPoolExecutor(1) as pool:
        previous = pool.submit(cli.main, ["verify-key"])
        try:
            assert entered.wait(timeout=8)
            set_key(settings.key_file, "fixture-new")
            assert cli.main(["verify-key"]) == 0
        finally:
            release.set()
        assert previous.result(timeout=8) == 1

    # 新設定維持就緒，兩筆已送請求均計入全域額度。
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert_setup(client, "ready")
    assert len(requests) == 2
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)


# 舊版帳本原地升級，兩個競爭的 CLI 都可越過舊上限並保留原值。
def test_cli_upgrades_legacy_ledger_and_records_concurrent_requests(
    settings: Settings,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
) -> None:
    requests, responses = mocked_provider
    set_key(settings.key_file, "fixture-secret")

    # 先用 CLI 留下一筆已就緒的紀錄，再重建舊版 PostgreSQL 限制及當日用量。
    responses.append(success)
    assert cli.main(["verify-key"]) == 0
    with settings.connect() as conn:
        conn.execute("UPDATE daily_model_quota SET used = 99")
        conn.execute(
            """INSERT INTO model_requests (id, service_day, delivery)
               SELECT gen_random_uuid(), service_day, 'sent'
               FROM daily_model_quota CROSS JOIN generate_series(1, 98)"""
        )
        conn.execute(
            """
            ALTER TABLE daily_model_quota
                DROP CONSTRAINT IF EXISTS daily_model_quota_used_nonnegative,
                DROP CONSTRAINT IF EXISTS daily_model_quota_used_check,
                ADD CONSTRAINT daily_model_quota_used_check CHECK (used BETWEEN 0 AND 100)
            """
        )
        original = conn.execute("SELECT * FROM model_requests ORDER BY id").fetchall()
        binding = conn.execute("SELECT * FROM activation").fetchone()

    # 初始化可重跑且保留歷史、驗證指紋及既有用量。
    initialize(settings)
    with ThreadPoolExecutor(1) as pool, settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (99,)
        pool.submit(initialize, settings).result(timeout=5)
        assert (
            conn.execute("SELECT * FROM model_requests ORDER BY id").fetchall()
            == original
        )
        assert conn.execute("SELECT * FROM activation").fetchone() == binding
    with pytest.raises(psycopg.errors.CheckViolation), settings.connect() as conn:
        conn.execute("UPDATE daily_model_quota SET used = -1")

    # 同步起跑，兩筆驗證請求都必須送出並原子增加原本的同日用量。
    barrier = Barrier(2, timeout=8)

    # 等兩個執行緒都到齊才執行同一個 CLI 入口。
    def run() -> int:
        barrier.wait()
        return cli.main(["verify-key"])

    responses.extend((success, success))
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(run)
        second = pool.submit(run)
        assert 0 in (first.result(timeout=8), second.result(timeout=8))
    assert len(requests) == 3

    # 新應用仍為就緒，讀回原有紀錄及兩筆已送出明細，重啟不會自動外呼。
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert_setup(client, "ready")
    assert len(requests) == 3
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (101,)
        assert conn.execute(
            "SELECT count(*) FROM model_requests WHERE delivery = 'sent'"
        ).fetchone() == (101,)
        assert set(original) <= set(conn.execute("SELECT * FROM model_requests"))


# 以原子預留的資料庫入口注入 UTC 時刻，驗證臺灣日期在邊界正確切換。
def test_quota_taipei_midnight(settings: Settings) -> None:
    # 固定在臺灣午夜前一瞬間及午夜整點，讓真實 SQL 選擇日期。
    before = datetime(2026, 11, 7, 15, 59, 59, 999999, tzinfo=UTC)
    midnight = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
    cancelled = uuid4()
    with settings.connect() as conn:
        conn.execute(
            "INSERT INTO daily_model_quota VALUES (%s, 100)", (date(2026, 11, 7),)
        )
        assert reserve_request(conn, cancelled, at=before) == date(2026, 11, 7)
        assert reserve_request(conn, uuid4(), at=midnight) == date(2026, 11, 8)

    # 新的日期有獨立計數，預留時先標為送達不明以防程序中止。
    with settings.connect() as conn:
        assert conn.execute(
            "SELECT service_day, used FROM daily_model_quota ORDER BY service_day"
        ).fetchall() == [(date(2026, 11, 7), 101), (date(2026, 11, 8), 1)]
        assert conn.execute(
            "SELECT service_day, delivery FROM model_requests ORDER BY service_day"
        ).fetchall() == [
            (date(2026, 11, 7), "unknown"),
            (date(2026, 11, 8), "unknown"),
        ]

    # 新日期已入帳後取消舊預留；跨交易重複結算也只退回原日一次。
    for _ in range(2):
        with settings.connect() as conn:
            settle_request(conn, cancelled, "unsent")
    with settings.connect() as conn:
        assert conn.execute(
            "SELECT service_day, used FROM daily_model_quota ORDER BY service_day"
        ).fetchall() == [(date(2026, 11, 7), 100), (date(2026, 11, 8), 1)]
        assert conn.execute(
            "SELECT service_day, delivery FROM model_requests ORDER BY service_day"
        ).fetchall() == [
            (date(2026, 11, 7), "unsent"),
            (date(2026, 11, 8), "unknown"),
        ]


# 檢查同源靜態輸出不會將未定義 API 改寫為 HTML 或讀出根目錄外檔案。
def test_static_boundary(settings: Settings, tmp_path: Path) -> None:
    # 模擬靜態輸出和不應被提供的相鄰檔案。
    public = tmp_path / "public"
    public.mkdir()
    (public / "index.html").write_text("<h1>啟用</h1>")
    secret = tmp_path / "secret"
    secret.write_text("fixture-private")
    (public / "unsafe").symlink_to(secret)

    # 只有合法檔案可同源讀取，穿越與不可信 Host 皆被拒絕。
    with TestClient(
        create_app(settings, public), base_url="http://localhost"
    ) as client:
        assert client.get("/").text == "<h1>啟用</h1>"
        assert client.get("/unsafe").status_code == 404
        assert client.get("/%2e%2e/secret").status_code == 404
        for path in ("/", "/api/setup", "/api/questions"):
            assert (
                client.get(path, headers={"host": "example.invalid"}).status_code == 400
            )
        assert (
            client.get("/api/questions").headers["content-type"] == "application/json"
        )


# PostgreSQL 不可用時，對 API 回傳安全錯誤並關閉業務入口。
def test_guard_database_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_key(settings.key_file, "fixture-secret")

    # 模擬已運作的服務遇到連線中斷，不把 libpq 原因送給瀏覽器。
    def disconnected(_: Settings) -> psycopg.Connection[tuple[object, ...]]:
        raise psycopg.OperationalError("fixture-secret")

    with TestClient(create_app(settings), base_url="http://localhost") as client:
        monkeypatch.setattr(Settings, "connect", disconnected)
        for path in ("/api/setup", "/api/questions"):
            response = client.get(path)
            assert response.status_code == 503
            assert "fixture-secret" not in response.text


@pytest.mark.parametrize("newer_succeeds", [False, True])
# 同一組 Key 競爭時，較新的明確結果勝出；較新的暫時錯誤不會掩蓋已知無效。
def test_same_key_result_order(
    settings: Settings,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
    newer_succeeds: bool,
) -> None:
    requests, responses = mocked_provider
    set_key(settings.key_file, "fixture-secret")
    responses.append(success)
    assert cli.main(["verify-key"]) == 0
    entered = Event()
    release = Event()

    # 先取得確定開始送出的舊請求，讓它在新請求結束後才回應。
    def delayed_invalid() -> httpx.Response:
        entered.set()
        assert release.wait(timeout=8)
        return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})

    # 新請求的限流屬暫時錯誤，與明確驗證成功分開測試。
    def limited() -> httpx.Response:
        return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})

    responses.extend((delayed_invalid, success if newer_succeeds else limited))
    with ThreadPoolExecutor(1) as pool:
        older = pool.submit(cli.main, ["verify-key"])
        try:
            assert entered.wait(timeout=8)
            assert cli.main(["verify-key"]) == (0 if newer_succeeds else 1)
        finally:
            release.set()
        assert older.result(timeout=8) == 1

    # 檢查最終 HTTP 業務門檻並確認三個已送請求均有計入。
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert_setup(client, "ready" if newer_succeeds else "invalid")
    assert len(requests) == 3
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (3,)
        assert conn.execute("SELECT delivery FROM model_requests").fetchall() == [
            ("sent",),
            ("sent",),
            ("sent",),
        ]


# 新 Key 在預留提交後、網路送出前生效時，可精確退回尚未送出的請求。
def test_config_changes_before_send(
    settings: Settings,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = mocked_provider
    set_key(settings.key_file, "fixture-old")
    reads = 0

    # 第三次讀取位於連線後、實際寫入前，模擬本機操作人員此時替換 Key。
    def rotated(path: Path) -> GeminiCredential | None:
        nonlocal reads
        reads += 1
        if reads == 3:
            set_key(path, "fixture-new")
        return read_key(path)

    # CLI 取消外呼，HTTP 持續封鎖，且資料庫留下確定未送出的分類。
    monkeypatch.setattr(activation, "read_key", rotated)
    assert cli.main(["verify-key"]) == 1
    assert not requests
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert_setup(client, "unverified")
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (0,)
        assert conn.execute("SELECT delivery FROM model_requests").fetchone() == (
            "unsent",
        )


# 預留期間已換 Key 時應在嘗試連線前取消，確定舊日額度可退回。
def test_cli_refunds_reservation_when_key_changed_before_connect(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_key(settings.key_file, "fixture-before-connect")
    connect_attempts: list[str] = []

    # 預留本身仍是實際 PostgreSQL 寫入；在舊憑證讀取後立即公布新的設定。
    def reserve_and_rotate(
        conn: psycopg.Connection[tuple[object, ...]],
        request_id: UUID,
    ) -> date | None:
        reserved_day = reserve_request(conn, request_id)
        set_key(settings.key_file, "fixture-after-reservation")
        return reserved_day

    # 若程式錯誤地嘗試連線，模擬接線前故障，讓尚未送出的預留不可被誤判。
    def fail_connect(
        _: httpcore.SyncBackend,
        host: str,
        port: int,
        **_kwargs: object,
    ) -> httpcore.NetworkStream:
        connect_attempts.append(host)
        raise httpcore.ConnectError("fixture connection failed")

    monkeypatch.setattr(activation, "reserve_request", reserve_and_rotate)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fail_connect)
    assert cli.main(["verify-key"]) == 1
    assert connect_attempts == []
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert_setup(client, "unverified")
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (0,)
        assert conn.execute("SELECT delivery FROM model_requests").fetchone() == (
            "unsent",
        )


# 損毀後即使用回同一把 Key，設定仍須取得新版本並經過明確驗證。
def test_repaired_key_requires_verification(
    settings: Settings,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
) -> None:
    requests, responses = mocked_provider
    key_value = "fixture-recovery"

    # 先確認資料庫確實有舊版本的成功驗證，才損毀私有檔案內容。
    set_key(settings.key_file, key_value)
    previous = read_key(settings.key_file)
    assert previous is not None
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        responses.append(success)
        assert cli.main(["verify-key"]) == 0
        assert_setup(client, "ready")
        settings.key_file.write_text(f"invalid-version\n{key_value}\n")
        assert_setup(client, "unverified")

        # 復原同一指紋也不能沿用舊版本的就緒狀態或自動發送模型請求。
        set_key(settings.key_file, key_value)
        repaired = read_key(settings.key_file)
        assert repaired is not None
        assert repaired.fingerprint == previous.fingerprint
        assert repaired.version != previous.version
        assert_setup(client, "unverified")
        assert len(requests) == 1

        # 只有新的明確驗證才放行，且只新增一筆已送出的請求。
        responses.append(success)
        assert cli.main(["verify-key"]) == 0
        assert_setup(client, "ready")
    assert len(requests) == 2
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (2,)
        assert conn.execute("SELECT delivery FROM model_requests").fetchall() == [
            ("sent",),
            ("sent",),
        ]


# 兩次同值設定交錯時，只能產生一個版本並保留先完成的啟用結果。
def test_concurrent_same_key_retains_ready(
    settings: Settings,
    mocked_provider: tuple[list[httpx.Request], list[Callable[[], httpx.Response]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, responses = mocked_provider
    set_key(settings.key_file, "fixture-old")
    key_value = "fixture-new"
    writing = Event()
    contended = Event()
    allow_first = Event()
    allow_second = Event()
    write = credentials.write_private
    flock = fcntl.flock

    # 暫停第一個寫入；若第二個也走到寫入，延到首次驗證完成才公布。
    def delayed_write(path: Path, content: str, *, replace: bool) -> None:
        if writing.is_set():
            contended.set()
            assert allow_second.wait(timeout=8)
        else:
            writing.set()
            assert allow_first.wait(timeout=8)
        write(path, content, replace=replace)

    # 第二個操作若使用鎖，先用獨立描述符證明第一個仍持鎖，再照常等待。
    def observe_flock(fd: int, operation: int) -> None:
        if writing.is_set():
            with pytest.raises(BlockingIOError):
                flock(fd, operation | fcntl.LOCK_NB)
            contended.set()
        flock(fd, operation)

    monkeypatch.setattr(credentials, "write_private", delayed_write)
    monkeypatch.setattr(fcntl, "flock", observe_flock)

    # 在第一個更新尚未提交時啟動第二個；只對第一個落盤的版本執行驗證。
    with (
        ThreadPoolExecutor(2) as pool,
        TestClient(create_app(settings), base_url="http://localhost") as client,
    ):
        first = pool.submit(set_key, settings.key_file, key_value)
        try:
            assert writing.wait(timeout=8)
            second = pool.submit(set_key, settings.key_file, key_value)
            assert contended.wait(timeout=8)
            allow_first.set()
            first.result(timeout=8)
            first_version = read_key(settings.key_file)
            assert first_version is not None
            assert_setup(client, "unverified")
            responses.append(success)
            assert cli.main(["verify-key"]) == 0
            assert_setup(client, "ready")
        finally:
            allow_first.set()
            allow_second.set()

        # 第二個操作完成後，先確認就緒仍然成立，再核對版本未改變。
        second.result(timeout=8)
        assert_setup(client, "ready")
        assert read_key(settings.key_file) == first_version

    # 設定操作沒有外呼；只有首次明確驗證用掉一筆額度。
    assert len(requests) == 1
    with settings.connect() as conn:
        assert conn.execute("SELECT used FROM daily_model_quota").fetchone() == (1,)
        assert conn.execute("SELECT delivery FROM model_requests").fetchall() == [
            ("sent",),
        ]


# 以真實預留交易模擬臺灣午夜前最後一瞬間，保留原請求及帳號。
def reserve_before_taipei_midnight(
    conn: psycopg.Connection[tuple[object, ...]],
    request_id: UUID,
    *,
    username: str | None = None,
) -> date:
    row = conn.execute(
        "SELECT date_trunc('day', clock_timestamp(), 'Asia/Taipei') - interval '1 microsecond'"
    ).fetchone()
    assert row is not None and isinstance(row[0], datetime)
    return reserve_request(conn, request_id, at=row[0], username=username)


# 在傳輸寫入邊界檢查已提交的預留日早於 PostgreSQL 所見臺灣日期。
def assert_previous_day_reservation(
    settings: Settings, username: str | None = None
) -> date:
    with settings.connect() as conn:
        row = conn.execute(
            """SELECT service_day, delivery, (clock_timestamp() AT TIME ZONE 'Asia/Taipei')::date
               FROM model_requests WHERE username IS NOT DISTINCT FROM %s""",
            (username,),
        ).fetchone()
    assert row is not None
    reserved_day, delivery, wire_day = row
    assert isinstance(reserved_day, date) and isinstance(wire_day, date)
    assert reserved_day < wire_day and delivery == "unknown"
    return reserved_day


# CLI 已跨臺灣日界仍可送出，且走真實同步 HTTP/1.1 寫入前檢查。
def test_cli_keeps_reservation_day_if_connection_crosses_taipei_midnight(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_key(settings.key_file, "fixture-rollover")
    writes: list[bytes] = []
    reserved_days: list[date] = []

    class FixtureStream(httpcore.NetworkStream):
        # 傳輸層寫入才算送出；無外部連線或憑證。
        def write(self, buffer: bytes, timeout: float | None = None) -> None:
            if not writes:
                reserved_days.append(assert_previous_day_reservation(settings))
            writes.append(buffer)

        # 供應商可正常驗證，讓測試區分跨日遭拒與成功送出。
        def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            body = success().content
            return (
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode("ascii") + body

        # 測試不持有真正的 socket。
        def close(self) -> None:
            return

        # 不接觸外部 TLS，只保留正式網域的傳輸契約。
        def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> httpcore.NetworkStream:
            assert server_hostname == "generativelanguage.googleapis.com"
            return self

    # 只替換 TCP 串流，保留 SDK 的 HTTP 寫入檢查。
    def connect(
        _: httpcore.SyncBackend, host: str, port: int, **_kwargs: object
    ) -> httpcore.NetworkStream:
        assert (host, port) == ("generativelanguage.googleapis.com", 443)
        return FixtureStream()

    # CLI 正常啟用並送出一筆請求；只有預留時間固定在午夜前。
    monkeypatch.setattr(activation, "reserve_request", reserve_before_taipei_midnight)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    assert cli.main(["verify-key"]) == 0
    assert b"".join(writes).count(b"POST /v1/interactions HTTP/1.1") == 1
    (original_day,) = reserved_days
    # 已送出維持原用量日；公開啟用狀態必須可用。
    with settings.connect() as conn:
        assert conn.execute(
            "SELECT service_day, used FROM daily_model_quota"
        ).fetchall() == [(original_day, 1)]
        assert conn.execute(
            "SELECT service_day, delivery FROM model_requests"
        ).fetchall() == [(original_day, "sent")]
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert_setup(client, "ready")
