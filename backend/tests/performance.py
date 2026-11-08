"""使用可拋棄的 PostgreSQL 及真實 HTTP 量測非模型 API。"""

import asyncio
import json
import logging
import math
import multiprocessing
import os
import platform
import socket
import sys
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID

import httpx
import psycopg
import uvicorn
from backend.app import create_app
from backend.credentials import DEMO_PASSWORDS_NAME, read_key, set_key, write_private
from backend.database import SCHEMA, Settings
from backend.knowledge import (
    Batch,
    Document,
    Fragment,
    SearchResponse,
    SearchResult,
    replace_documents,
)
from backend.provider import MODEL, PROVIDER
from backend.sessions import Identity, Role, initialize_accounts, utc_now
from backend.tests.conftest import SUPPORT_PASSWORD, TEST_DSN, USER_PASSWORD
from backend.tickets import TicketDetail, TicketPage, TicketSummary
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

RATE = 10
SECONDS = 60
PROJECT_DB = "ai-support-perf-db-1"
SEED_TICKETS = 999
DOCUMENTS = 50
PAGE_SIZE = 50
Kind = Literal["search", "list", "detail", "create", "close"]
LIMIT_MS: dict[Kind, int] = {
    "search": 500,
    "list": 500,
    "detail": 500,
    "create": 1000,
    "close": 1000,
}
PER_KIND = RATE * SECONDS // len(LIMIT_MS)
MEASURED_CREATE = SEED_TICKETS + 1
OTHER_OWNER = MEASURED_CREATE + PER_KIND
PAGE_OFFSET = PER_KIND + 1
HISTORICAL_TIME = datetime(2020, 1, 1, tzinfo=UTC)
IMMUTABLE_TABLES = (
    "activation",
    "knowledge_documents",
    "conversations",
    "questions",
    "answer_attempts",
    "ticket_attempt_replays",
    "model_requests",
    "daily_model_quota",
)


class BenchmarkError(Exception):
    """僅保存固定、安全的診斷。"""


@dataclass
class Observation:
    sequence: int
    kind: Kind
    case: int
    scheduled_s: float | None = None
    dispatched_s: float | None = None
    headers_started_s: float | None = None
    sent_s: float | None = None
    body_read_s: float | None = None
    duration_ms: float | None = None
    status: int | None = None
    list_shift: int | None = None
    load_error: str | None = None
    http_error: str | None = None
    data_error: str | None = None


# 每篇文件有可預期的中文子字串及完整版本證據。
def document_result(index: int) -> SearchResult:
    return SearchResult(
        document_id=f"perf-{index:02}",
        title=f"測試文件 {index:02}",
        version="fixture-v1",
        fragment_id="guide",
        excerpt=f"【路由{index:02}】裝置測試步驟。",
    )


# 種子工單與未建單來源共用固定識別及期望快照；建單時由 HTTP 產生實際單號。
def fixture_ticket(index: int) -> TicketDetail:
    result = document_result(index % DOCUMENTS)
    answered = index % 2 == 0
    return TicketDetail(
        id=UUID(int=index + 1),
        question_id=UUID(int=index + 2001),
        attempt_id=UUID(int=index + 4001),
        question=f"測試問題 {index:04}",
        description=f"測試描述 {index:04}",
        answer_status="answered" if answered else "insufficient_evidence",
        answer=result.excerpt if answered else None,
        citations=[result] if answered else [],
        search_results=[result],
        created_by="support"
        if (index < SEED_TICKETS and index % 2 == 0) or index == OTHER_OWNER
        else "user",
        created_at=HISTORICAL_TIME + timedelta(seconds=index),
        status="open",
        resolution=None,
        closed_at=None,
    )


@contextmanager
# 連到獨立 Compose 專案的精確 DNS 目標，核對伺服器及空白 schema 才允許寫入。
def empty_fixture(directory: Path) -> Iterator[Settings]:
    address = socket.gethostbyname(PROJECT_DB)
    settings = Settings(
        directory / "api-key",
        make_conninfo(
            TEST_DSN,
            host=PROJECT_DB,
            hostaddr=address,
            port=5432,
            options="-csearch_path=public",
        ),
        directory / DEMO_PASSWORDS_NAME,
    )
    with settings.connect() as conn:
        # 持有專用工作階段鎖至整次測試完成，避免兩個基準同時初始化。
        conn.autocommit = True
        identity = conn.execute(
            """
            SELECT current_database(), current_user, current_setting('ai_support.test_fixture', true),
                   current_setting('server_version_num')::int / 10000,
                   host(inet_server_addr()), inet_server_port()
            """
        ).fetchone()
        if identity != (
            "ai_support_test",
            "ai_support_test",
            "disposable-ticket01",
            18,
            address,
            5432,
        ):
            raise BenchmarkError("unexpected_disposable_database_identity")
        if conn.execute(
            "SELECT pg_try_advisory_lock(7680281636712970529)"
        ).fetchone() != (True,):
            raise BenchmarkError("fixture_in_use")

        # 連任何 public 業務物件都不接收，避免接手已用過或非空的測試庫。
        if (
            conn.execute(
                """
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' LIMIT 1
            """
            ).fetchone()
            is not None
        ):
            raise BenchmarkError("fixture_schema_already_exists")

        # 同一個連線及交易在驗證後建立 schema；離開時只關連線，不清除資料。
        with conn.transaction():
            conn.execute(SCHEMA.read_text(encoding="utf-8"))
        yield settings


# 僅預置安全的虛構啟用、文件及建單前置資料，量測中的異動一律走公開 API。
def seed(settings: Settings) -> dict[UUID, TicketDetail]:
    # 走產品相同的本機憑證、帳號與文件驗證，金鑰沒有外部用途。
    set_key(settings.key_file, "benchmark-fixture-no-provider-credential")
    key = read_key(settings.key_file)
    if key is None:
        raise BenchmarkError("missing_synthetic_key")
    write_private(
        settings.demo_passwords_file,
        f"user={USER_PASSWORD}\nsupport={SUPPORT_PASSWORD}\n",
        replace=False,
    )
    initialize_accounts(settings)
    results = [document_result(index) for index in range(DOCUMENTS)]
    replace_documents(
        settings,
        Batch(
            documents=[
                Document(
                    id=result.document_id,
                    title=result.title,
                    version=result.version,
                    fragments=[Fragment(id=result.fragment_id, text=result.excerpt)],
                )
                for result in results
            ]
        ),
    )

    # 暖機建單補至 1,000 張。每次新建各用一段仍有效的對話，沒有外部模型工作。
    existing = [fixture_ticket(index) for index in range(SEED_TICKETS)]
    sources = {
        UUID(int=index + 6001): fixture_ticket(index)
        for index in range(SEED_TICKETS, OTHER_OWNER + 1)
    }
    now = utc_now()
    with settings.connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE activation SET key_version = %s, fingerprint = %s, provider = %s,
                model = %s, status = 'ready', probe_sequence = 1, verified_sequence = 1
            WHERE singleton
            """,
            (key.version, key.fingerprint, PROVIDER, MODEL),
        )
        cursor.executemany(
            """
            INSERT INTO conversations (id, username, created_at, last_seen_at, key_version)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                (cid, row.created_by, now, now, key.version)
                for cid, row in sources.items()
            ),
        )
        cursor.executemany(
            "INSERT INTO questions (id, conversation_id, question) VALUES (%s, %s, %s)",
            ((row.question_id, cid, row.question) for cid, row in sources.items()),
        )
        cursor.executemany(
            """
            INSERT INTO answer_attempts (id, question_id, status, answer, citations, search_results)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                (
                    row.attempt_id,
                    row.question_id,
                    row.answer_status,
                    row.answer,
                    Jsonb([item.model_dump() for item in row.citations]),
                    Jsonb([item.model_dump() for item in row.search_results]),
                )
                for row in sources.values()
            ),
        )
        cursor.executemany(
            """
            INSERT INTO tickets (id, question_id, attempt_id, description, question, answer_status,
                answer, citations, search_results, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                (
                    row.id,
                    row.question_id,
                    row.attempt_id,
                    row.description,
                    row.question,
                    row.answer_status,
                    row.answer,
                    Jsonb([item.model_dump() for item in row.citations]),
                    Jsonb([item.model_dump() for item in row.search_results]),
                    row.created_by,
                    row.created_at,
                )
                for row in existing
            ),
        )
        cursor.execute("ANALYZE tickets")
        cursor.execute("ANALYZE knowledge_documents")
    return {row.id: row for row in existing}


# 讓 uvicorn 在獨立程序處理真正 TCP 請求，避免與負載排程共用事件迴圈。
def serve(settings: Settings, listener: socket.socket) -> None:
    logging.disable(logging.CRITICAL)
    try:
        config = uvicorn.Config(
            create_app(settings),
            http="h11",
            ws="none",
            loop="asyncio",
            log_config=None,
            access_log=False,
            proxy_headers=False,
            timeout_graceful_shutdown=2,
        )
        uvicorn.Server(config).run(sockets=[listener])
    except Exception:
        raise SystemExit(1) from None


@asynccontextmanager
# 只綁定容器內 loopback 的臨時埠，等待該子程序的 HTTP 健康檢查並有界收尾。
async def local_server(settings: Settings) -> AsyncIterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        process = multiprocessing.get_context("spawn").Process(
            target=serve, args=(settings, listener), daemon=True
        )
        process.start()

    origin = f"http://127.0.0.1:{port}"
    try:
        # 啟動完成才進入登入及暖機；健康檢查不接觸模型。
        async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
            deadline = asyncio.get_running_loop().time() + 15
            while process.is_alive() and asyncio.get_running_loop().time() < deadline:
                try:
                    if (await client.get(f"{origin}/health")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise BenchmarkError("http_server_not_ready")
        yield origin
    finally:
        # 只終止自己建立的子程序，確保私有設定在服務退出後才刪除。
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        if process.is_alive():
            raise BenchmarkError("http_server_did_not_stop")
        process.close()


# 直接讀 PostgreSQL 的全部工單及預期唯讀表，核對量測期間未產生旁支資料。
def database_snapshot(
    settings: Settings,
) -> tuple[dict[UUID, TicketDetail], dict[str, list[tuple[object, ...]]]]:
    with settings.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with conn.cursor(row_factory=class_row(TicketDetail)) as cursor:
            rows = cursor.execute("SELECT * FROM tickets").fetchall()
        tables = {
            table: conn.execute(
                sql.SQL("SELECT to_jsonb(t)::text FROM {} AS t ORDER BY 1").format(
                    sql.Identifier(table)
                )
            ).fetchall()
            for table in IMMUTABLE_TABLES
        }
    return {row.id: row for row in rows}, tables


# 使用獨立角色的真實 Cookie 工作階段，登入不計入效能。
async def login(client: httpx.AsyncClient, role: Role, password: str) -> None:
    response = await client.post(
        "/api/login", json={"username": role, "password": password}
    )
    if response.status_code != 200:
        raise BenchmarkError(f"login_{role}_http_{response.status_code}")
    if Identity.model_validate_json(response.content) != Identity(
        username=role, role=role
    ):
        raise BenchmarkError(f"login_{role}_identity_mismatch")


# 送出的建單欄位只包含識別及描述，回覆內原始證據必須由後端補齊。
def create_body(index: int) -> dict[str, str]:
    row = fixture_ticket(index)
    return {
        "question_id": str(row.question_id),
        "attempt_id": str(row.attempt_id),
        "description": row.description,
    }


# 結案填入每張工單專屬的處理結果，便於辨識串寫或覆蓋。
def resolution(index: int) -> str:
    return f"測試結案結果 {index:04}"


# 確認有區辨力的拒絕案例；預期拒絕不列入效能錯誤或請求量。
async def check_boundaries(
    origin: str, user: httpx.AsyncClient, support: httpx.AsyncClient
) -> dict[str, tuple[int, int]]:
    own = fixture_ticket(1).id
    other = fixture_ticket(0).id
    async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=5) as guest:
        checks = {
            "anonymous_list": ((await guest.get("/api/tickets")).status_code, 401),
            "cross_owner_detail": (
                (await user.get(f"/api/tickets/{other}")).status_code,
                404,
            ),
            "cross_owner_create": (
                (
                    await user.post("/api/tickets", json=create_body(OTHER_OWNER))
                ).status_code,
                404,
            ),
            "user_close": (
                (
                    await user.post(
                        f"/api/tickets/{own}/close", json={"resolution": resolution(1)}
                    )
                ).status_code,
                403,
            ),
            "blank_resolution": (
                (
                    await support.post(
                        f"/api/tickets/{own}/close", json={"resolution": " "}
                    )
                ).status_code,
                422,
            ),
            "forged_snapshot": (
                (
                    await user.post(
                        "/api/tickets",
                        json={**create_body(MEASURED_CREATE), "answer": "偽造"},
                    )
                ).status_code,
                422,
            ),
        }
    return checks


# 依角色比對完整摘要，辨識新建工單出現在固定歷史頁之前造成的位移。
def list_shift(
    response: httpx.Response, case: int, pages: dict[Role, list[TicketSummary]]
) -> int | None:
    page = TicketPage.model_validate_json(response.content)
    role: Role = "user" if case % 2 else "support"
    rows = pages[role]
    if page.has_more:
        for offset in range(PAGE_OFFSET + 1):
            if page.tickets == rows[offset : offset + PAGE_SIZE]:
                return PAGE_OFFSET - offset
    return None


# 按固定案例選擇實際 API；查詢及處理結果使用合成資料。
def request_parts(
    case: int, kind: Kind
) -> tuple[Role, str, str, dict[str, str] | None]:
    match kind:
        case "search":
            return (
                "user",
                "POST",
                "/api/knowledge/search",
                {
                    "query": "查無此路由"
                    if case % 5 == 4
                    else f"路由{case % DOCUMENTS:02}"
                },
            )
        case "list":
            role: Role = "user" if case % 2 else "support"
            return (
                role,
                "GET",
                f"/api/tickets?limit={PAGE_SIZE}&offset={PAGE_OFFSET}",
                None,
            )
        case "detail":
            row = fixture_ticket(2 * PER_KIND + case)
            return row.created_by, "GET", f"/api/tickets/{row.id}", None
        case "create":
            index = SEED_TICKETS if case == -1 else MEASURED_CREATE + case
            return "user", "POST", "/api/tickets", create_body(index)
        case "close":
            index = case + 1
            return (
                "support",
                "POST",
                f"/api/tickets/{fixture_ticket(index).id}/close",
                {"resolution": resolution(index)},
            )


# 驗證每筆成功回應；建單與結案另保存核對過的 DTO，供獨立資料庫讀回比較。
def check_response(
    response: httpx.Response,
    sample: Observation,
    pages: dict[Role, list[TicketSummary]],
    writes: dict[int, TicketDetail],
    started_at: datetime,
) -> bool:
    kind, case = sample.kind, sample.case
    if kind == "search":
        expected = [] if case % 5 == 4 else [document_result(case % DOCUMENTS)]
        return SearchResponse.model_validate_json(response.content).results == expected
    if kind == "list":
        sample.list_shift = list_shift(response, case, pages)
        return sample.list_shift is not None

    # 排除只有本次交易才能決定的值後，核對快照所有欄位。
    result = TicketDetail.model_validate_json(response.content)
    if kind == "detail":
        return result == fixture_ticket(2 * PER_KIND + case)
    if kind == "create":
        index = SEED_TICKETS if case == -1 else MEASURED_CREATE + case
        expected_ticket = fixture_ticket(index).model_copy(
            update={"id": result.id, "created_at": result.created_at}
        )
        if (
            result != expected_ticket
            or not started_at <= result.created_at <= utc_now()
        ):
            return False
    else:
        expected_ticket = fixture_ticket(case + 1).model_copy(
            update={
                "status": "closed",
                "resolution": resolution(case + 1),
                "closed_at": result.closed_at,
            }
        )
        if (
            result != expected_ticket
            or result.closed_at is None
            or not started_at <= result.closed_at <= utc_now()
        ):
            return False
    writes[sample.sequence] = result
    return True


# 記錄客戶端開始、標頭開始寫入、請求寫完及回應本文收完的時點。
async def attempt(
    sample: Observation,
    clients: dict[Role, httpx.AsyncClient],
    pages: dict[Role, list[TicketSummary]],
    writes: dict[int, TicketDetail],
    start: float,
) -> None:
    loop = asyncio.get_running_loop()
    role, method, path, payload = request_parts(sample.case, sample.kind)

    # 標頭開始只用於因果範圍；HTTP/1.1 寫完請求本文才計入送出量。
    async def trace(event: str, _: dict[str, object]) -> None:
        if event == "http11.send_request_headers.started":
            sample.headers_started_s = loop.time() - start
        elif event == "http11.send_request_body.complete":
            sample.sent_s = loop.time() - start

    # 半個時槽後即放棄逾時排程，避免靠後續突發送出補足數量。
    now = loop.time()
    dispatched = now - start
    if sample.scheduled_s is not None and dispatched - sample.scheduled_s >= 0.5 / RATE:
        sample.load_error = "dispatch_slot_missed"
        return
    sample.dispatched_s = dispatched
    started_at = utc_now()
    try:
        async with asyncio.timeout(8):
            response = await clients[role].request(
                method, path, json=payload, extensions={"trace": trace}
            )
    except (httpx.RequestError, TimeoutError) as error:
        sample.http_error = type(error).__name__
        return

    # 讀完本文才停止計時；檢核回應、統計與資料庫讀回均在量測邊界之外。
    read_at = loop.time()
    sample.body_read_s = read_at - start
    sample.duration_ms = (read_at - now) * 1000
    sample.status = response.status_code
    if response.status_code != 200:
        sample.http_error = "unexpected_status"
        return
    # 傳輸層可能在寫入失敗後仍讀到回應，照常檢核內容但保留送出失敗。
    if sample.sent_s is None:
        sample.http_error = "request_write_incomplete"
    try:
        if not check_response(response, sample, pages, writes, started_at):
            sample.data_error = "response_mismatch"
    except (ValidationError, ValueError):
        sample.data_error = "invalid_response_payload"


@dataclass
class Report:
    environment: dict[str, object]
    warmup: list[Observation] = field(default_factory=list)
    attempts: list[Observation] = field(default_factory=list)
    boundaries: dict[str, tuple[int, int]] = field(default_factory=dict)
    initial: dict[str, int] = field(default_factory=dict)
    final: dict[str, int] = field(default_factory=dict)
    load: dict[str, object] = field(default_factory=dict)
    categories: dict[Kind, dict[str, object]] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


# 以固定單調時鐘時槽獨立安排請求，慢回應不會拖住後續發送。
async def run_load(
    report: Report,
    clients: dict[Role, httpx.AsyncClient],
    pages: dict[Role, list[TicketSummary]],
    writes: dict[int, TicketDetail],
) -> None:
    loop = asyncio.get_running_loop()
    start = loop.time() + 0.1
    tasks: list[asyncio.Task[None]] = []
    for case in range(PER_KIND):
        for kind in LIMIT_MS:
            sequence = len(report.attempts)
            due = (sequence + 0.5) / RATE
            sample = Observation(sequence, kind, case, scheduled_s=due)
            report.attempts.append(sample)
            await asyncio.sleep(max(0, start + due - loop.time()))
            tasks.append(
                asyncio.create_task(attempt(sample, clients, pages, writes, start))
            )

    # 等足完整量測窗，再收完已送出的本文，兩段時間分開報告。
    await asyncio.sleep(max(0, start + SECONDS - loop.time()))
    await asyncio.gather(*tasks)
    elapsed_s = loop.time() - start

    # 用標頭開始作為請求最早可能被伺服器接收的界線，避免把重疊的建單判成錯誤。
    # 清單標頭開始前已回應的建單必須可見；其回應收完前開始的建單可能增加位移。
    # 暖機工單對兩個角色皆可見，因此上下界均從 1 開始。
    creates = [sample for sample in report.attempts if sample.kind == "create"]
    for sample in report.attempts:
        if (
            sample.list_shift is None
            or sample.headers_started_s is None
            or sample.body_read_s is None
        ):
            continue
        earliest, latest = sample.headers_started_s, sample.body_read_s
        minimum = 1 + sum(
            create.sequence in writes
            and create.body_read_s is not None
            and create.body_read_s < earliest
            for create in creates
        )
        maximum = 1 + sum(
            create.headers_started_s is not None and create.headers_started_s < latest
            for create in creates
        )
        if not minimum <= sample.list_shift <= maximum:
            sample.data_error = "unexpected_list_page_shift"
    summarize(report, elapsed_s)


# 以排序後向上取整的實際樣本秩計算 p95，不插值、不以顯示精度裁決。
def nearest_p95(values: list[float]) -> float:
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


# 分開判斷是否送足負載、是否全部完成，以及各類延遲和錯誤。
def summarize(report: Report, elapsed_s: float) -> None:
    # 每秒及每個 100 毫秒時槽各須足量，且相鄰傳送不能壓到 50 毫秒內補發。
    offered = sorted(
        sample.sent_s for sample in report.attempts if sample.sent_s is not None
    )
    seconds = Counter(math.floor(sent) for sent in offered)
    gaps = [current - previous for previous, current in zip(offered, offered[1:])]
    valid_slots = sum(
        sample.sent_s is not None
        and math.floor(sample.sent_s * RATE) == sample.sequence
        for sample in report.attempts
    )
    if (
        len(offered) != RATE * SECONDS
        or valid_slots != RATE * SECONDS
        or any(seconds[second] != RATE for second in range(SECONDS))
        or min(gaps, default=0) < 0.5 / RATE
    ):
        report.failures.append("insufficient_or_uneven_offered_load")
    report.load = {
        "scheduled": len(report.attempts),
        "offered": len(offered),
        "completed": sum(sample.status is not None for sample in report.attempts),
        "valid_send_slots": valid_slots,
        "offered_per_second": [seconds[second] for second in range(SECONDS)],
        "first_send_s": offered[0] if offered else None,
        "last_send_s": offered[-1] if offered else None,
        "min_send_gap_ms": min(gaps) * 1000 if gaps else None,
        "scheduled_window_s": SECONDS,
        "wall_including_drain_s": elapsed_s,
    }

    # 保留每類錯誤與 p95；缺樣本或失敗回應即使 p95 達標也不能通過。
    for kind, limit in LIMIT_MS.items():
        samples = [sample for sample in report.attempts if sample.kind == kind]
        durations = [
            sample.duration_ms for sample in samples if sample.duration_ms is not None
        ]
        p95 = nearest_p95(durations) if durations else None
        http_errors = sum(sample.http_error is not None for sample in samples)
        data_errors = sum(sample.data_error is not None for sample in samples)
        load_errors = sum(sample.load_error is not None for sample in samples)
        report.categories[kind] = {
            "scheduled": len(samples),
            "offered": sum(sample.sent_s is not None for sample in samples),
            "completed": len(durations),
            "http_errors": http_errors,
            "data_errors": data_errors,
            "load_errors": load_errors,
            "p95_ms": p95,
            "limit_ms": limit,
        }
        if (
            len(durations) != PER_KIND
            or http_errors + data_errors + load_errors != 0
            or p95 is None
            or p95 > limit
        ):
            report.failures.append(f"{kind}_acceptance_failed")


# 在前後兩階段查資料庫，逐列確認完整工單及唯讀表，偵測遺失、額外新增或未預期覆寫。
def verify_database(
    settings: Settings,
    expected: dict[UUID, TicketDetail],
    untouched: dict[str, list[tuple[object, ...]]],
    failures: list[str],
    phase: str,
) -> dict[str, int]:
    tickets, tables = database_snapshot(settings)
    if tickets != expected:
        failures.append(f"{phase}_ticket_readback_mismatch")
    failures.extend(
        f"{phase}_unexpected_{table}_change"
        for table in IMMUTABLE_TABLES
        if tables[table] != untouched[table]
    )
    return {
        "tickets": len(tickets),
        "closed": sum(row.status == "closed" for row in tickets.values()),
        "documents": len(tables["knowledge_documents"]),
        "model_requests": len(tables["model_requests"]),
        "quota_rows": len(tables["daily_model_quota"]),
    }


# 先驗證角色、暖機及起始資料，再量測五種請求並核對最終持久狀態。
async def benchmark(
    settings: Settings, seeded: dict[UUID, TicketDetail], report: Report
) -> None:
    # 取未受並行新增影響的歷史頁，依角色各自建立期望順序。
    summaries = [
        TicketSummary.model_validate(row.model_dump())
        for row in reversed(seeded.values())
    ]
    pages: dict[Role, list[TicketSummary]] = {
        "support": summaries,
        "user": [row for row in summaries if seeded[row.id].created_by == "user"],
    }
    tickets, untouched = database_snapshot(settings)
    if (
        tickets != seeded
        or len(untouched["knowledge_documents"]) != DOCUMENTS
        or untouched["model_requests"]
        or untouched["daily_model_quota"]
        or untouched["ticket_attempt_replays"]
    ):
        raise BenchmarkError("invalid_seed_data")

    # 每個角色使用獨立 HTTP 連線池與 Cookie；避免低連線上限人為壓低負載。
    async with local_server(settings) as origin:
        headers = {"Origin": origin, "X-CSRF-Protection": "1"}
        async with (
            httpx.AsyncClient(
                base_url=origin, headers=headers, trust_env=False, timeout=5
            ) as user,
            httpx.AsyncClient(
                base_url=origin, headers=headers, trust_env=False, timeout=5
            ) as support,
        ):
            await login(user, "user", USER_PASSWORD)
            await login(support, "support", SUPPORT_PASSWORD)
            report.boundaries = await check_boundaries(origin, user, support)
            report.failures.extend(
                f"boundary_{name}_mismatch"
                for name, (actual, expected) in report.boundaries.items()
                if actual != expected
            )

            # 暖機異動與正式樣本分離，暖機建單補足第 1,000 張。
            clients: dict[Role, httpx.AsyncClient] = {"user": user, "support": support}
            writes: dict[int, TicketDetail] = {}
            warm_start = asyncio.get_running_loop().time()
            for index, kind in enumerate(LIMIT_MS, start=1):
                sample = Observation(-index, kind, -1)
                report.warmup.append(sample)
                await attempt(sample, clients, pages, writes, warm_start)
                if kind == "list" and sample.list_shift != 0:
                    sample.data_error = "unexpected_list_page_shift"
                if (
                    sample.sent_s is None
                    or sample.http_error is not None
                    or sample.data_error is not None
                ):
                    report.failures.append(f"warmup_{kind}_failed")

            expected = seeded | {row.id: row for row in writes.values()}
            report.initial = verify_database(
                settings, expected, untouched, report.failures, "initial"
            )
            if report.initial != {
                "tickets": SEED_TICKETS + 1,
                "closed": 1,
                "documents": DOCUMENTS,
                "model_requests": 0,
                "quota_rows": 0,
            }:
                report.failures.append("wrong_measured_start_counts")
            if report.failures:
                return

            # 即使效能失敗，也核對實際寫入；每種異動應各有 120 筆獨立結果。
            writes.clear()
            await run_load(report, clients, pages, writes)
            if (
                len(writes) != 2 * PER_KIND
                or len({row.id for row in writes.values()}) != 2 * PER_KIND
            ):
                report.failures.append("wrong_measured_write_count")
            expected.update((row.id, row) for row in writes.values())
            report.final = verify_database(
                settings, expected, untouched, report.failures, "final"
            )
            if report.final != {
                **report.initial,
                "tickets": SEED_TICKETS + 1 + PER_KIND,
                "closed": 1 + PER_KIND,
            }:
                report.failures.append("wrong_final_counts")


# 只保存實際可讀取的容器資源欄位；主機及背景負載由操作者記錄。
def environment() -> dict[str, object]:
    resources: dict[str, object] = {
        "container_machine": platform.machine(),
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
    }
    for name in ("cpu.max", "cpuset.cpus.effective", "memory.max"):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            resources[name] = path.read_text(encoding="ascii").strip()
    return resources


# 無論是否達標都輸出可保存的單一 JSON，只有安全的分類診斷會離開測試。
def main() -> int:
    report = Report({})
    try:
        # 120 筆樣本的 p95 必須取第 114 筆；分頁契約驗證則在暖機及量測執行。
        if nearest_p95([float(value) for value in range(120)]) != 113:
            raise BenchmarkError("incorrect_nearest_rank")
        report.environment = environment()
        with TemporaryDirectory(prefix="ai-support-performance-") as directory:
            with empty_fixture(Path(directory)) as settings:
                seeded = seed(settings)
                asyncio.run(benchmark(settings, seeded, report))
    except BenchmarkError as error:
        report.failures.append(str(error))
    except (OSError, psycopg.Error, httpx.RequestError, ValidationError) as error:
        report.failures.append(type(error).__name__)
    except KeyboardInterrupt:
        report.failures.append("interrupted")
    finally:
        # 保留已觀察到的時槽與錯誤，供失敗結果追查。
        print(json.dumps(asdict(report), ensure_ascii=False, allow_nan=False))
    return int(bool(report.failures))


if __name__ == "__main__":
    sys.exit(main())
