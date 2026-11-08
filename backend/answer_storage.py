from dataclasses import dataclass
from datetime import datetime, timedelta
from math import floor
from threading import Event
from time import monotonic
from typing import Literal, cast
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from backend import knowledge, sessions
from backend.activation import activation_binding
from backend.credentials import GeminiCredential
from backend.database import (
    Settings,
    lock_request_binding,
    record_request_rejection,
    settle_request,
)
from backend.provider import BUSINESS_WRITE_SECONDS, MODEL, Delivery

CONVERSATION_IDLE = timedelta(minutes=30)
CONVERSATION_MAX = timedelta(hours=2)
SEND_RECHECK_SECONDS = 3
# HTTP/1.1 最多寫入標頭、單一 JSON 本文及結尾；另留三秒給鎖釋放及排程。
REJECTION_FENCE_WAIT_SECONDS = SEND_RECHECK_SECONDS + 3 * BUSINESS_WRITE_SECONDS + 3
Dispatch = tuple[psycopg.Connection[tuple[object, ...]], int, float]
AnswerStatus = Literal[
    "answered",
    "insufficient_evidence",
    "invalid_output",
    "rate_limited",
    "timed_out",
    "unavailable",
]


class AnswerResponse(BaseModel):
    conversation_id: UUID
    question_id: UUID
    attempt_id: UUID
    status: AnswerStatus
    answer: str | None = None
    model: str = MODEL
    citations: list[knowledge.SearchResult]
    search_results: list[knowledge.SearchResult]


@dataclass(frozen=True)
class Access:
    identity: sessions.Identity
    key: GeminiCredential
    sequence: int


class AnswerDenied(Exception):
    # 保留受控拒絕原因供 HTTP 轉接；不回顯來源資料或模型內容。
    def __init__(self, code: int, detail: str) -> None:
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Attempt:
    conversation_id: UUID
    question_id: UUID
    id: UUID
    access: Access


class AttemptDeadline(Exception):
    pass


# 過期後禁止發起新的工具或外呼；用單調時鐘避免系統時間校正延長嘗試。
def require_time(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise AttemptDeadline
    return remaining


# 每次鎖等待最多三秒；工具查詢也受這次問答實際剩餘的 SQL 執行時間限制。
def bound_transaction(
    conn: psycopg.Connection[tuple[object, ...]], deadline: float | None = None
) -> None:
    if deadline is None:
        conn.execute("SET LOCAL statement_timeout = '3s'")
    else:
        milliseconds = max(1, floor(min(3, require_time(deadline)) * 1000))
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)", (str(milliseconds),)
        )


# 取得 Session 再鎖啟用，等到最後一把授權鎖才檢查期限；拒絕也能提交憑證撤銷。
def authorize(
    conn: psycopg.Connection[tuple[object, ...]],
    settings: Settings,
    digest: bytes,
    *,
    refresh: bool = False,
    deadline: float | None = None,
) -> Access | AnswerDenied:
    # 沿用搜尋的鎖順序，已撤銷或到期的登入不可讀取啟用與私人資料。
    if sessions.read_active_session(conn, digest, refresh=False) is None:
        return AnswerDenied(401, "請先登入。")

    # 等待 Session 後重新計算剩餘時間，才可取得啟用鎖並讀取現有 Key。
    if deadline is not None:
        bound_transaction(conn, deadline)
    setup, key, sequence = activation_binding(settings, lock_with=conn)
    if key is None:
        return AnswerDenied(503, setup.message)

    # 啟用列可能等待 CLI；有效才允許更新登入的閒置期限。
    if deadline is not None:
        bound_transaction(conn, deadline)
    identity = sessions.read_active_session(conn, digest, refresh=refresh)
    if identity is None:
        return AnswerDenied(401, "請先登入。")
    return Access(identity, key, sequence)


# 建立單輪對話、問題及執行識別；先驗證身分，且三筆在同一交易提交。
def start_attempt(settings: Settings, digest: bytes, question: str) -> Attempt:
    with settings.connect() as conn:
        bound_transaction(conn)
        access = authorize(conn, settings, digest, refresh=True)
        if isinstance(access, Access):
            attempt = Attempt(uuid4(), uuid4(), uuid4(), access)
            at = sessions.utc_now()
            conn.execute(
                """
                INSERT INTO conversations (id, username, created_at, last_seen_at)
                VALUES (%s, %s, %s, %s)
                """,
                (attempt.conversation_id, access.identity.username, at, at),
            )
            conn.execute(
                "INSERT INTO questions (id, conversation_id, question) VALUES (%s, %s, %s)",
                (attempt.question_id, attempt.conversation_id, question),
            )
            conn.execute(
                "INSERT INTO answer_attempts (id, question_id, status) VALUES (%s, %s, 'running')",
                (attempt.id, attempt.question_id),
            )
            return attempt

    # 若到期，先提交撤銷再回傳拒絕。
    raise access


# 寫入 HTTP 請求前取鎖，僅成功時把連線交給傳輸層持有到請求寫完。
def lock_dispatch(
    settings: Settings, digest: bytes, expected: Access, deadline: float
) -> Dispatch:
    conn = settings.connect()
    try:
        # 不以先前預留時的狀態代替送出當下的登入與啟用檢查。
        bound_transaction(conn, deadline)
        access = authorize(conn, settings, digest, deadline=deadline)
        if isinstance(access, Access) and access.key == expected.key:
            # 先拿 Session／啟用列鎖才等綁定共用鎖；拒絕交易完全不等這兩種列鎖。
            bound_transaction(conn, deadline)
            lock_request_binding(conn, access.key.version, access.sequence, shared=True)

            # 拿到共用鎖後讀取最新拒絕和 Session；共用鎖持有到本機寫完才釋放。
            wire_deadline = min(deadline, monotonic() + SEND_RECHECK_SECONDS)
            bound_transaction(conn, wire_deadline)
            access = authorize(conn, settings, digest, deadline=wire_deadline)
            if isinstance(access, Access) and access.key == expected.key:
                return conn, access.sequence, wire_deadline

        # 拒絕時提交到期工作階段的撤銷；換 Key 後不把舊前文交給新 Key。
        conn.commit()
        if isinstance(access, AnswerDenied):
            raise access
        raise AnswerDenied(503, "Gemini 設定已變更，請重新提出問題。")
    except BaseException:
        # 包含工作中斷；只有成功回傳的連線會由傳輸層負責釋放。
        conn.close()
        raise


# 搜尋由伺服器授權並使用與公開搜尋相同的查詢；讀取後再次確認工作階段期限。
def search_attempt(
    settings: Settings, digest: bytes, attempt: Attempt, query: str, deadline: float
) -> list[knowledge.SearchResult]:
    with settings.connect() as conn:
        bound_transaction(conn, deadline)
        access = authorize(conn, settings, digest, deadline=deadline)
        if isinstance(access, Access) and access.key == attempt.access.key:
            # 授權可能用掉大部分時間；以當下剩餘時間重新設定真正的查詢期限。
            bound_transaction(conn, deadline)
            require_time(deadline)
            results = knowledge.find_fragments(conn, query)
            require_time(deadline)

            # 只有期限內且仍經授權的真實結果可交給模型或保留為證據。
            access = authorize(conn, settings, digest, deadline=deadline)
            if isinstance(access, Access) and access.key == attempt.access.key:
                require_time(deadline)
                return results

    # 確定失效時不把已讀結果提供給模型或瀏覽器。
    if isinstance(access, AnswerDenied):
        raise access
    raise AnswerDenied(503, "Gemini 設定已變更，請重新提出問題。")


# 已確認拒絕只鎖原請求帳本，與送達狀態一起提交；未確認的請求維持原有結算。
def record_delivery(
    settings: Settings,
    request_id: UUID,
    delivery: Delivery,
    rejected: tuple[UUID, int] | None,
) -> None:
    if delivery == "unknown" and rejected is None:
        return

    # 拒絕只等待已獲准的本機寫入；整個交易不取啟用列鎖或等待遠端模型回覆。
    try:
        with settings.connect() as conn:
            if rejected is not None:
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{REJECTION_FENCE_WAIT_SECONDS}s",),
                )
                lock_request_binding(conn, *rejected, shared=False)

                # 一旦取得排他鎖，回到一般資料庫期限，提交後才允許新送出查核。
                bound_transaction(conn)
                record_request_rejection(conn, request_id, *rejected)
            else:
                bound_transaction(conn)
                settle_request(conn, request_id, delivery)
    except psycopg.Error:
        if rejected is not None:
            raise AnswerDenied(503, "目前無法確認 Gemini 啟用狀態。") from None
        raise


# 結束已停止的執行；狀態條件確保不覆蓋已存在的終態。
def stop_attempt(settings: Settings, attempt: Attempt) -> None:
    with settings.connect() as conn:
        bound_transaction(conn)
        conn.execute(
            "UPDATE answer_attempts SET status = 'stopped' WHERE id = %s AND status = 'running'",
            (attempt.id,),
        )


# 逾時只保留在期限內已取得的真實搜尋結果。
def mark_timed_out(response: AnswerResponse) -> None:
    response.status = "timed_out"
    response.answer = None
    response.citations = []


# 鎖定私人資料後再確認授權；只有仍屬於呼叫者的執行可寫入及交付終態。
def finish_attempt(
    settings: Settings,
    digest: bytes,
    attempt: Attempt,
    response: AnswerResponse,
    deadline: float,
    disconnected: Event,
) -> None:
    with settings.connect() as conn:
        bound_transaction(conn)
        # 順序為 Session、啟用、對話，再寫入嘗試；清理只從對話開始。
        access = authorize(conn, settings, digest)
        row = None
        if isinstance(access, Access) and access.key == attempt.access.key:
            row = conn.execute(
                """
                SELECT created_at, last_seen_at FROM conversations
                WHERE id = %s AND username = %s FOR UPDATE
                """,
                (attempt.conversation_id, access.identity.username),
            ).fetchone()

            # 先依對話再嘗試的順序持鎖，與到期清理一致；所有列到手後重新授權。
            if row is not None:
                conn.execute(
                    "SELECT id FROM answer_attempts WHERE id = %s FOR UPDATE",
                    (attempt.id,),
                )
                access = authorize(conn, settings, digest)

        # 等待最後一把資料列鎖後才驗證期限；拒絕仍讓工作階段到期的刪除提交。
        if (
            isinstance(access, Access)
            and access.key == attempt.access.key
            and row is not None
        ):
            at = sessions.utc_now()
            created, last_seen = cast(tuple[datetime, datetime], row)
            if created <= at - CONVERSATION_MAX or last_seen <= at - CONVERSATION_IDLE:
                raise AnswerDenied(410, "問答暫存已到期。")

            # HTTP 已斷線時不寫回答；等鎖超過總期限時只保存已取得的搜尋結果。
            if disconnected.is_set():
                raise AnswerDenied(499, "已停止等待。")
            if response.status == "timed_out" or monotonic() >= deadline:
                mark_timed_out(response)

            # 單一交易更新活動時間和內容快照；知識庫改版不會改寫引用。
            updated = conn.execute(
                """
                UPDATE answer_attempts SET status = %s, answer = %s, citations = %s, search_results = %s
                WHERE id = %s AND status = 'running'
                """,
                (
                    response.status,
                    response.answer,
                    Jsonb([source.model_dump() for source in response.citations]),
                    Jsonb([source.model_dump() for source in response.search_results]),
                    attempt.id,
                ),
            )
            if updated.rowcount != 1:
                raise AnswerDenied(404, "找不到這次問答。")
            conn.execute(
                "UPDATE conversations SET last_seen_at = GREATEST(last_seen_at, %s) WHERE id = %s",
                (at, attempt.conversation_id),
            )

            # 資料寫入也可能等待；提交前若超時，於同一交易撤掉未公布的回答與引用。
            if response.status != "timed_out" and monotonic() >= deadline:
                mark_timed_out(response)
                conn.execute(
                    "UPDATE answer_attempts SET status = 'timed_out', answer = NULL, citations = '[]' WHERE id = %s",
                    (attempt.id,),
                )

            # 若在 SQL 寫入期間觀察到斷線，回滾私人內容並由呼叫端結束嘗試。
            if disconnected.is_set():
                raise AnswerDenied(499, "已停止等待。")
            return

    # 登出、失效或換 Key 均不可交付已取得的私人結果。
    if isinstance(access, AnswerDenied):
        raise access
    if access.key != attempt.access.key:
        raise AnswerDenied(503, "Gemini 設定已變更，請重新提出問題。")
    raise AnswerDenied(404, "找不到這次問答。")


# 只讀取指定且屬於本人的嘗試；不延長對話或登入期限。
def read_attempt(settings: Settings, digest: bytes, attempt_id: UUID) -> AnswerResponse:
    with settings.connect() as conn:
        bound_transaction(conn)
        # 資源識別只是定位條件，還要通過登入、啟用和擁有者限制。
        access = authorize(conn, settings, digest)
        row = None
        if isinstance(access, Access):
            row = conn.execute(
                """
                SELECT c.id, q.id, c.created_at, c.last_seen_at,
                       a.status, a.answer, a.citations, a.search_results
                FROM answer_attempts a
                JOIN questions q ON q.id = a.question_id
                JOIN conversations c ON c.id = q.conversation_id
                WHERE a.id = %s AND c.username = %s FOR SHARE OF c
                """,
                (attempt_id, access.identity.username),
            ).fetchone()
            access = authorize(conn, settings, digest)

    # 若在等候資料庫時失效，拒絕交付已查到的資料。
    if isinstance(access, AnswerDenied):
        raise access
    if row is None:
        raise AnswerDenied(404, "找不到這次問答。")
    (
        conversation_id,
        question_id,
        created,
        last_seen,
        status,
        answer,
        citations,
        results,
    ) = row
    at = sessions.utc_now()
    if (
        cast(datetime, created) <= at - CONVERSATION_MAX
        or cast(datetime, last_seen) <= at - CONVERSATION_IDLE
    ):
        raise AnswerDenied(410, "問答暫存已到期。")

    # 只公開已完成的嘗試；對外 DTO 排除工作狀態與任何供應商內部步驟。
    if status in ("running", "stopped"):
        raise AnswerDenied(404, "找不到這次問答。")
    return AnswerResponse.model_validate(
        {
            "conversation_id": conversation_id,
            "question_id": question_id,
            "attempt_id": attempt_id,
            "status": status,
            "answer": answer,
            "citations": citations,
            "search_results": results,
        }
    )


# 週期和啟動清理共用同一條件；刪除的範圍只有到期的對話及其從屬資料。
def purge_expired(settings: Settings) -> None:
    at = sessions.utc_now()
    with settings.connect() as conn:
        bound_transaction(conn)
        conn.execute(
            "DELETE FROM conversations WHERE created_at <= %s OR last_seen_at <= %s",
            (at - CONVERSATION_MAX, at - CONVERSATION_IDLE),
        )
