from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import floor
from threading import Event
from time import monotonic
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

import psycopg
from google.genai._gaos.types.interactions import StepParam
from psycopg.types.json import Jsonb
from pydantic import BaseModel, BeforeValidator, Field, TypeAdapter

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
QUESTION_LIMIT = 10
NOT_FOUND = "找不到這次問答。"
EXPIRED = "問答暫存已到期。"
SIGN_IN = "請先登入。"
CONVERSATION_BUSY = "這段對話正在處理問題，請稍後再試。"
KEY_CHANGED = "Gemini 設定已變更，請重新提出問題。"
SEND_RECHECK_SECONDS = 3
# HTTP/1.1 最多寫入標頭、單一 JSON 本文及結尾；另留三秒給鎖釋放及排程。
REJECTION_FENCE_WAIT_SECONDS = SEND_RECHECK_SECONDS + 3 * BUSINESS_WRITE_SECONDS + 3
Dispatch = tuple[psycopg.Connection[tuple[object, ...]], int, float, datetime]
AnswerStatus = Literal[
    "answered",
    "insufficient_evidence",
    "invalid_output",
    "rate_limited",
    "timed_out",
    "unavailable",
]
SEARCH_RESULTS: TypeAdapter[list[knowledge.SearchResult]] = TypeAdapter(
    list[knowledge.SearchResult]
)


# 原始 JSON 的識別必須是標準 UUID 字串，不能把空值視為新對話。
def parse_answer_id(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("問答識別格式不正確。")
    identifier = UUID(value)
    if str(identifier) != value.lower():
        raise ValueError("問答識別格式不正確。")
    return identifier


AnswerIdentifier = Annotated[UUID, BeforeValidator(parse_answer_id)]


class NewQuestion(knowledge.KnowledgeInput):
    question: knowledge.PlainText = Field(max_length=2000)
    conversation_id: Annotated[UUID | None, BeforeValidator(parse_answer_id)] = None


class RetryQuestion(knowledge.KnowledgeInput):
    conversation_id: AnswerIdentifier
    question_id: AnswerIdentifier
    attempt_id: AnswerIdentifier


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
    session_expires_at: datetime


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
    question: str
    history: list[StepParam]


@dataclass(frozen=True)
class AttemptWindow:
    conversation_expires: datetime
    attempt_expires: datetime


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
        return AnswerDenied(401, SIGN_IN)

    # 等待 Session 後重新計算剩餘時間，才可取得啟用鎖並讀取現有 Key。
    if deadline is not None:
        bound_transaction(conn, deadline)
    setup, key, sequence = activation_binding(settings, lock_with=conn)
    if key is None:
        return AnswerDenied(503, setup.message)

    # 啟用列可能等待 CLI；有效才允許更新登入的閒置期限。
    if deadline is not None:
        bound_transaction(conn, deadline)
    session = sessions.read_locked_session(conn, digest, refresh=refresh)
    if session is None:
        return AnswerDenied(401, SIGN_IN)
    identity, expires_at = session
    return Access(identity, key, sequence, expires_at)


# 不讀取未經擁有者驗證的對話；忙碌拒絕只讀已提交資料，不必等列鎖。
def lock_conversation(
    conn: psycopg.Connection[tuple[object, ...]],
    conversation_id: UUID,
    username: str,
    *,
    update: bool | None,
) -> tuple[datetime, datetime, UUID | None] | None:
    lock = "" if update is None else ("FOR UPDATE" if update else "FOR SHARE")
    return cast(
        tuple[datetime, datetime, UUID | None] | None,
        conn.execute(
            f"""
            SELECT created_at, last_seen_at, key_version FROM conversations
            WHERE id = %s AND username = %s {lock}
            """,
            (conversation_id, username),
        ).fetchone(),
    )


# 以閒置和絕對期限較早者為準；等到取鎖後才和目前時間比較。
def conversation_expiry(created: datetime, last_seen: datetime) -> datetime:
    return min(created + CONVERSATION_MAX, last_seen + CONVERSATION_IDLE)


# 只針對自己的對話檢查到期和 Key；忙碌快查也必須遵守同一拒絕順序。
def admit_conversation_window(
    row: tuple[datetime, datetime, UUID | None] | None, access: Access
) -> datetime:
    if row is None:
        raise AnswerDenied(404, NOT_FOUND)
    created, last_seen, key_version = row
    expiry = conversation_expiry(created, last_seen)
    if sessions.utc_now() >= expiry:
        raise AnswerDenied(410, EXPIRED)
    if key_version is not None and key_version != access.key.version:
        raise AnswerDenied(503, KEY_CHANGED)
    return expiry


# 只載入較早成功問題的完整交換，不重建簽章或採信失敗嘗試的部分結果。
def load_history(
    conn: psycopg.Connection[tuple[object, ...]], conversation_id: UUID, position: int
) -> list[StepParam]:
    rows = conn.execute(
        """
        SELECT a.model_steps FROM questions q
        JOIN answer_attempts a ON a.question_id = q.id
        WHERE q.conversation_id = %s AND q.position < %s AND a.status = 'answered'
        ORDER BY q.position
        """,
        (conversation_id, position),
    ).fetchall()

    # 舊回答仍可讀取，但無原始步驟就不能冒充完整的可信前文。
    if any(steps is None for (steps,) in rows):
        raise AnswerDenied(409, "這段對話缺少可延續的前文，請開始新的對話。")
    return [step for (steps,) in rows for step in cast(list[StepParam], steps)]


# 先驗證身分再鎖對話；登入已到期時，先提交撤銷再回傳拒絕。
def start_attempt(
    settings: Settings,
    digest: bytes,
    form: NewQuestion | RetryQuestion,
    deadline_at: datetime,
) -> Attempt:
    with settings.connect() as conn:
        # 同一 Session 的終態交易也可能持有 Session 鎖。先看已提交的忙碌狀態，只供後續拒絕。
        was_busy = False
        if form.conversation_id is not None:
            # 授權須先讀帳本才鎖對話。先結束快查交易，避免帶著對話表鎖進入授權。
            with conn.transaction():
                bound_transaction(conn)
                was_busy = busy_at_arrival(conn, digest, form.conversation_id)
        bound_transaction(conn)
        access = authorize(conn, settings, digest)
        if isinstance(access, Access):
            try:
                # 最後一次寫入等待中若到期，回滾整次受理並保留外層的 Session 撤銷。
                with conn.transaction():
                    result = admit_attempt(
                        conn, settings, digest, access, form, deadline_at, was_busy
                    )
                    if isinstance(result, AnswerDenied):
                        raise result
                    return result
            except AnswerDenied as denied:
                if denied.code != 401:
                    raise
                sessions.revoke_session(conn, digest)
                access = denied
    raise access


# 只查 Cookie 摘要所屬的對話是否有未到期執行；此結果不作為授權或受理依據。
def busy_at_arrival(
    conn: psycopg.Connection[tuple[object, ...]], digest: bytes, conversation_id: UUID
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM login_sessions s JOIN conversations c ON c.username = s.username
        JOIN questions q ON q.conversation_id = c.id
        JOIN answer_attempts a ON a.question_id = q.id
        WHERE s.token_digest = %s AND c.id = %s AND a.status = 'running'
            AND a.deadline_at > clock_timestamp()
        LIMIT 1
        """,
        (digest, conversation_id),
    ).fetchone()
    return row is not None


# 同一把對話鎖序列化新題、重試和終態；只替已逾期的中斷嘗試釋出席位。
def admit_attempt(
    conn: psycopg.Connection[tuple[object, ...]],
    settings: Settings,
    digest: bytes,
    access: Access,
    form: NewQuestion | RetryQuestion,
    deadline_at: datetime,
    was_busy: bool,
) -> Attempt | AnswerDenied:
    conversation_id = form.conversation_id
    expiry = None
    if conversation_id is not None:
        # 忙碌請求仍須驗證擁有者、期限及 Key；可能受理的請求則必須重新持鎖檢查。
        row = lock_conversation(
            conn,
            conversation_id,
            access.identity.username,
            update=None if was_busy else True,
        )
        current = authorize(conn, settings, digest)
        if isinstance(current, AnswerDenied):
            return current
        expiry = admit_conversation_window(row, current)
        if was_busy:
            raise AnswerDenied(409, CONVERSATION_BUSY)
        access = current

        # 資料庫期限跨程序有效；舊版沒有期限的執行不能在新程序繼續。
        conn.execute(
            """
            UPDATE answer_attempts a SET status = 'stopped' FROM questions q
            WHERE q.id = a.question_id AND q.conversation_id = %s AND a.status = 'running'
                AND (a.deadline_at IS NULL OR a.deadline_at <= clock_timestamp())
            """,
            (conversation_id,),
        )
        busy = conn.execute(
            """
            SELECT 1 FROM answer_attempts a JOIN questions q ON q.id = a.question_id
            WHERE q.conversation_id = %s AND a.status = 'running' LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if busy is not None:
            raise AnswerDenied(409, CONVERSATION_BUSY)

    # 新題才配置新位置；重試只接受精確隸屬於對話的目前失敗嘗試。
    if isinstance(form, RetryQuestion):
        question, position, number = retry_details(conn, form)
        question_id = form.question_id
    else:
        position = 1
        if conversation_id is not None:
            row_count = conn.execute(
                "SELECT count(*) FROM questions WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
            position = cast(tuple[int], row_count)[0] + 1
        if position > QUESTION_LIMIT:
            raise AnswerDenied(409, "這段對話已達 10 題上限，請開始新的對話。")
        question_id, question, number = uuid4(), form.question.strip(), 1
    history = (
        [] if conversation_id is None else load_history(conn, conversation_id, position)
    )

    # 通過狀態檢查才刷新 Session；等候資料期間到期或換 Key 仍不可提交新嘗試。
    current = authorize(conn, settings, digest, refresh=True)
    if isinstance(current, AnswerDenied):
        return current
    if current.key != access.key:
        raise AnswerDenied(503, KEY_CHANGED)
    at = sessions.utc_now()
    if expiry is not None and at >= expiry:
        raise AnswerDenied(410, EXPIRED)
    if conversation_id is None:
        conversation_id = uuid4()
        conn.execute(
            """
            INSERT INTO conversations (id, username, created_at, last_seen_at, key_version)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (conversation_id, access.identity.username, at, at, access.key.version),
        )
    else:
        # 舊版列沒有綁定與模型步驟；新內容從這次已驗證設定開始保存。
        conn.execute(
            """
            UPDATE conversations SET last_seen_at = GREATEST(last_seen_at, %s), key_version = %s
            WHERE id = %s
            """,
            (at, access.key.version, conversation_id),
        )

    # 問題及執行識別在同一交易提交；重試不改寫原題或占用新位置。
    if isinstance(form, NewQuestion):
        conn.execute(
            "INSERT INTO questions (id, conversation_id, question, position) VALUES (%s, %s, %s, %s)",
            (question_id, conversation_id, question, position),
        )
    attempt_id = uuid4()
    conn.execute(
        """
        INSERT INTO answer_attempts (id, question_id, status, attempt_number, deadline_at)
        VALUES (%s, %s, 'running', %s, %s)
        """,
        (attempt_id, question_id, number, deadline_at),
    )

    # 受理識別也屬於私人資料；SQL 寫完後、提交及交付之前再次確認授權與期限。
    final_access = authorize(conn, settings, digest)
    if isinstance(final_access, AnswerDenied):
        return final_access
    if final_access.key != current.key:
        raise AnswerDenied(503, KEY_CHANGED)
    if expiry is not None and sessions.utc_now() >= expiry:
        raise AnswerDenied(410, EXPIRED)
    return Attempt(
        conversation_id, question_id, attempt_id, final_access, question, history
    )


# 用完整識別定位自有原題，再以資料庫嘗試序號拒絕過時或已成功的重試。
def retry_details(
    conn: psycopg.Connection[tuple[object, ...]], form: RetryQuestion
) -> tuple[str, int, int]:
    row = conn.execute(
        """
        SELECT q.question, q.position, a.attempt_number, a.status,
            NOT EXISTS (SELECT 1 FROM answer_attempts newer
                        WHERE newer.question_id = q.id AND newer.attempt_number > a.attempt_number)
        FROM questions q JOIN answer_attempts a ON a.question_id = q.id
        WHERE q.conversation_id = %s AND q.id = %s AND a.id = %s
        """,
        (form.conversation_id, form.question_id, form.attempt_id),
    ).fetchone()
    if row is None:
        raise AnswerDenied(404, NOT_FOUND)
    question, position, number, status, current = cast(
        tuple[str, int, int, str, bool], row
    )
    if not current or status == "answered":
        raise AnswerDenied(409, "這次問答已無法重試。")
    return question, position, number + 1


# 外呼和搜尋都先鎖對話、再鎖執行列；持鎖期間不會有新嘗試取代這筆執行。
def lock_running_attempt(
    conn: psycopg.Connection[tuple[object, ...]], attempt: Attempt, deadline: float
) -> AttemptWindow | None:
    row = lock_conversation(
        conn, attempt.conversation_id, attempt.access.identity.username, update=False
    )
    if row is None:
        return None

    # 對話鎖可能等待；取得後重新依剩餘時間限制執行列的查詢。
    bound_transaction(conn, deadline)
    running = conn.execute(
        """
        SELECT deadline_at FROM answer_attempts WHERE id = %s AND question_id = %s
            AND status = 'running' FOR SHARE
        """,
        (attempt.id, attempt.question_id),
    ).fetchone()
    if running is None or not isinstance(running[0], datetime):
        return None
    return AttemptWindow(conversation_expiry(row[0], row[1]), running[0])


# 到期對話不可讀寫；僅執行期限到期則依既有流程回傳逾時終態。
def check_attempt_window(window: AttemptWindow | None) -> float:
    if window is None:
        raise AnswerDenied(404, NOT_FOUND)
    conversation_remaining = (
        window.conversation_expires - sessions.utc_now()
    ).total_seconds()
    if conversation_remaining <= 0:
        raise AnswerDenied(410, EXPIRED)
    attempt_remaining = (window.attempt_expires - datetime.now(UTC)).total_seconds()
    if attempt_remaining <= 0:
        raise AttemptDeadline
    return min(conversation_remaining, attempt_remaining)


# 寫入 HTTP 請求前取鎖，僅成功時把連線交給傳輸層持有到請求寫完。
def lock_dispatch(
    settings: Settings, digest: bytes, attempt: Attempt, deadline: float
) -> Dispatch:
    conn = settings.connect()
    try:
        # 不以先前預留時的狀態代替送出當下的登入與啟用檢查。
        bound_transaction(conn, deadline)
        access = authorize(conn, settings, digest, deadline=deadline)
        if isinstance(access, Access) and access.key == attempt.access.key:
            # 先拿 Session／啟用列鎖才等綁定共用鎖；拒絕交易完全不等這兩種列鎖。
            bound_transaction(conn, deadline)
            lock_request_binding(conn, access.key.version, access.sequence, shared=True)

            wire_deadline = min(deadline, monotonic() + SEND_RECHECK_SECONDS)
            bound_transaction(conn, wire_deadline)
            window = lock_running_attempt(conn, attempt, wire_deadline)

            # 資料列到手後讀取最新拒絕和 Session；鎖都持有到本機寫完才釋放。
            access = authorize(conn, settings, digest, deadline=wire_deadline)
            if isinstance(access, Access) and access.key == attempt.access.key:
                now = monotonic()
                remaining = check_attempt_window(window)
                return (
                    conn,
                    access.sequence,
                    min(wire_deadline, now + remaining),
                    access.session_expires_at,
                )

        # 拒絕時提交到期工作階段的撤銷；換 Key 後不把舊前文交給新 Key。
        conn.commit()
        if isinstance(access, AnswerDenied):
            raise access
        raise AnswerDenied(503, KEY_CHANGED)
    except BaseException:
        # 包含工作中斷；只有成功回傳的連線會由傳輸層負責釋放。
        conn.close()
        raise


# 傳輸層仍持有 Session 列鎖；送出前發現到期時先提交撤銷再釋放鎖。
def revoke_dispatch_session(
    conn: psycopg.Connection[tuple[object, ...]], digest: bytes
) -> None:
    sessions.revoke_session(conn, digest)
    conn.commit()


# 搜尋由伺服器授權並使用與公開搜尋相同的查詢；讀取後再次確認工作階段期限。
def search_attempt(
    settings: Settings, digest: bytes, attempt: Attempt, query: str, deadline: float
) -> list[knowledge.SearchResult]:
    with settings.connect() as conn:
        bound_transaction(conn, deadline)
        access = authorize(conn, settings, digest, deadline=deadline)
        if isinstance(access, Access) and access.key == attempt.access.key:
            bound_transaction(conn, deadline)
            window = lock_running_attempt(conn, attempt, deadline)
            access = authorize(conn, settings, digest, deadline=deadline)
            if isinstance(access, Access) and access.key == attempt.access.key:
                # 等候最後一把鎖也會消耗期限，且過期或被接手的執行不得再搜尋。
                query_deadline = min(
                    deadline, monotonic() + check_attempt_window(window)
                )
                bound_transaction(conn, query_deadline)
                results = knowledge.find_fragments(conn, query)
                require_time(query_deadline)

                # 只有期限內且仍經授權的真實結果可交給模型或保留為證據。
                access = authorize(conn, settings, digest, deadline=query_deadline)
                if isinstance(access, Access) and access.key == attempt.access.key:
                    check_attempt_window(window)
                    require_time(query_deadline)
                    try:
                        # 搜尋完成就保存快照；建單及終態寫入不依賴執行緒收到這次回傳值。
                        with conn.transaction():
                            if results:
                                persist_search_results(conn, attempt.id, results)
                            access = authorize(
                                conn, settings, digest, deadline=query_deadline
                            )
                            if isinstance(access, AnswerDenied):
                                raise access
                            if access.key != attempt.access.key:
                                raise AnswerDenied(503, KEY_CHANGED)
                            check_attempt_window(window)
                            require_time(query_deadline)
                        return results
                    except AnswerDenied as denied:
                        if denied.code != 401:
                            raise
                        # 私人快照回滾，已到期的登入仍須撤銷。
                        sessions.revoke_session(conn, digest)
                        access = denied

    # 確定失效時不把已讀結果提供給模型或瀏覽器。
    if isinstance(access, AnswerDenied):
        raise access
    raise AnswerDenied(503, KEY_CHANGED)


# 此執行只有一個搜尋寫入者，且對話共用鎖擋住停止和終態，依取得順序保留不重複的證據。
def persist_search_results(
    conn: psycopg.Connection[tuple[object, ...]],
    attempt_id: UUID,
    results: list[knowledge.SearchResult],
) -> None:
    row = conn.execute(
        "SELECT search_results FROM answer_attempts WHERE id = %s", (attempt_id,)
    ).fetchone()
    if row is None:
        raise AnswerDenied(404, NOT_FOUND)
    evidence = SEARCH_RESULTS.validate_python(row[0])
    for result in results:
        if result not in evidence:
            evidence.append(result)
    conn.execute(
        "UPDATE answer_attempts SET search_results = %s WHERE id = %s",
        (Jsonb([source.model_dump() for source in evidence]), attempt_id),
    )


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


# 只鎖自己的執行列收尾；不取得對話鎖，也不覆蓋已存在的終態。
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
    model_steps: list[StepParam] | None,
    deadline: float,
    disconnected: Event,
) -> None:
    with settings.connect() as conn:
        bound_transaction(conn)
        # 順序為 Session、啟用、對話，再寫入嘗試；清理只從對話開始。
        access = authorize(conn, settings, digest)
        row = None
        running = None
        if isinstance(access, Access) and access.key == attempt.access.key:
            row = lock_conversation(
                conn, attempt.conversation_id, access.identity.username, update=True
            )

            # 先依對話再嘗試的順序持鎖，與到期清理一致；所有列到手後重新授權。
            if row is not None:
                running = conn.execute(
                    """
                    SELECT deadline_at, search_results FROM answer_attempts
                    WHERE id = %s AND status = 'running' FOR UPDATE
                    """,
                    (attempt.id,),
                ).fetchone()
                access = authorize(conn, settings, digest)

        # 等待最後一把資料列鎖後才驗證期限；拒絕仍讓工作階段到期的刪除提交。
        if (
            isinstance(access, Access)
            and access.key == attempt.access.key
            and row is not None
        ):
            at = sessions.utc_now()
            expiry = conversation_expiry(row[0], row[1])
            if at >= expiry:
                raise AnswerDenied(410, EXPIRED)
            if running is None or not isinstance(running[0], datetime):
                raise AnswerDenied(404, NOT_FOUND)
            deadline_at = running[0]
            response.search_results = SEARCH_RESULTS.validate_python(running[1])

            # HTTP 已斷線時不寫回答；等鎖超過總期限時只保存已取得的搜尋結果。
            if disconnected.is_set():
                raise AnswerDenied(499, "已停止等待。")
            if (
                response.status == "timed_out"
                or monotonic() >= deadline
                or datetime.now(UTC) >= deadline_at
            ):
                mark_timed_out(response)

            # 私人寫入放在儲存點內，到期時可回滾資料並在外層交易提交撤銷。
            try:
                with conn.transaction():
                    # 單一交易更新活動時間和內容快照；知識庫改版不會改寫引用。
                    updated = conn.execute(
                        """
                        UPDATE answer_attempts SET status = %s, answer = %s, citations = %s,
                            model_steps = %s
                        WHERE id = %s AND status = 'running'
                        """,
                        (
                            response.status,
                            response.answer,
                            Jsonb(
                                [source.model_dump() for source in response.citations]
                            ),
                            Jsonb(model_steps)
                            if response.status == "answered"
                            else None,
                            attempt.id,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise AnswerDenied(404, NOT_FOUND)
                    conn.execute(
                        "UPDATE conversations SET last_seen_at = GREATEST(last_seen_at, %s) WHERE id = %s",
                        (at, attempt.conversation_id),
                    )

                    # 最後寫入也可能等待；確認登入和啟用仍有效，才允許提交私人結果。
                    current = authorize(conn, settings, digest)
                    if isinstance(current, AnswerDenied):
                        raise current
                    if current.key != attempt.access.key:
                        raise AnswerDenied(503, KEY_CHANGED)

                    # 資料寫入也可能等待；提交前若超時，於同一交易撤掉未公布的回答與引用。
                    if response.status != "timed_out" and (
                        monotonic() >= deadline or datetime.now(UTC) >= deadline_at
                    ):
                        mark_timed_out(response)
                        conn.execute(
                            """
                            UPDATE answer_attempts SET status = 'timed_out', answer = NULL,
                                citations = '[]', model_steps = NULL WHERE id = %s
                            """,
                            (attempt.id,),
                        )

                    # 若在 SQL 寫入期間觀察到斷線，回滾私人內容並由呼叫端結束嘗試。
                    if disconnected.is_set():
                        raise AnswerDenied(499, "已停止等待。")
                    if sessions.utc_now() >= expiry:
                        raise AnswerDenied(410, EXPIRED)

                    # 最後一次 SQL 完成後才決定能否提交和交付。Session 列仍鎖定，期限不會被其他請求刷新。
                    if sessions.utc_now() >= current.session_expires_at:
                        raise AnswerDenied(401, SIGN_IN)
            except AnswerDenied as denied:
                if denied.code != 401:
                    raise
                sessions.revoke_session(conn, digest)
                access = denied
            else:
                return

    # 登出、失效或換 Key 均不可交付已取得的私人結果。
    if isinstance(access, AnswerDenied):
        raise access
    if access.key != attempt.access.key:
        raise AnswerDenied(503, KEY_CHANGED)
    raise AnswerDenied(404, NOT_FOUND)


# 只讀取指定且屬於本人的嘗試；不延長對話或登入期限。
def read_attempt(settings: Settings, digest: bytes, attempt_id: UUID) -> AnswerResponse:
    with settings.connect() as conn:
        bound_transaction(conn)
        # 資源識別只是定位條件，還要通過登入、啟用和擁有者限制。
        access = authorize(conn, settings, digest)
        row = None
        window = None
        if isinstance(access, Access):
            conversation = conn.execute(
                """
                SELECT c.id
                FROM answer_attempts a
                JOIN questions q ON q.id = a.question_id
                JOIN conversations c ON c.id = q.conversation_id
                WHERE a.id = %s AND c.username = %s
                """,
                (attempt_id, access.identity.username),
            ).fetchone()
            if conversation is not None:
                window = lock_conversation(
                    conn,
                    cast(UUID, conversation[0]),
                    access.identity.username,
                    update=False,
                )
                # 等候對話鎖可能跨過登入期限，先重驗授權才能讀取私人欄位。
                access = authorize(conn, settings, digest)
            if window is not None and isinstance(access, Access):
                # 等到對話鎖及授權均確認後再取新快照，以免讀到等待期間已被重試的舊嘗試。
                row = conn.execute(
                    """
                    SELECT q.conversation_id, q.id, a.status, a.answer, a.citations, a.search_results
                    FROM answer_attempts a JOIN questions q ON q.id = a.question_id
                    WHERE a.id = %s AND NOT EXISTS (
                        SELECT 1 FROM answer_attempts newer
                        WHERE newer.question_id = q.id AND newer.attempt_number > a.attempt_number
                    )
                    """,
                    (attempt_id,),
                ).fetchone()
                # 最後讀取完成後才重新確認登入與啟用；撤銷可隨這筆交易提交。
                access = authorize(conn, settings, digest)

    # 若在等候資料庫時失效，拒絕交付已查到的資料。
    if isinstance(access, AnswerDenied):
        raise access
    if row is None or window is None:
        raise AnswerDenied(404, NOT_FOUND)
    conversation_id, question_id, status, answer, citations, results = row
    created, last_seen, _ = window
    if sessions.utc_now() >= conversation_expiry(created, last_seen):
        raise AnswerDenied(410, EXPIRED)

    # 只公開已完成的嘗試；對外 DTO 排除工作狀態與任何供應商內部步驟。
    if status in ("running", "stopped"):
        raise AnswerDenied(404, NOT_FOUND)
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
