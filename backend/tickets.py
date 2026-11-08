from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import class_row
from pydantic import BaseModel, Field, field_serializer

from backend import knowledge, sessions
from backend.answer_storage import (
    EXPIRED,
    AnswerDenied,
    AnswerIdentifier,
    AnswerStatus,
    authorize,
    bound_transaction,
    conversation_expiry,
    lock_conversation,
    parse_answer_id,
)
from backend.database import Settings

PAGE_LIMIT = 100
OFFSET_LIMIT = 2_147_483_647
NOT_FOUND = "找不到這張工單或指定的問答。"
STALE_ATTEMPT = "這次問答已更新，請使用目前的問答建立工單。"
CLOSE_FORBIDDEN = "只有客服可以結案。"
ALREADY_CLOSED = "這張工單已結案。"
TicketConnection = Connection[tuple[object, ...]]


class TicketInput(knowledge.KnowledgeInput):
    question_id: AnswerIdentifier
    attempt_id: AnswerIdentifier
    description: knowledge.PlainText = Field(max_length=4000)


# 僅接受必填純文字，並保留處理結果原有的空白和換行。
class ResolutionInput(knowledge.KnowledgeInput):
    resolution: knowledge.PlainText = Field(max_length=4000)


# 列表與明細共用原始問題及結案欄位，尚未結案時處理結果與時間皆為空值。
class TicketSummary(BaseModel):
    id: UUID
    description: str
    question: str
    created_at: datetime
    status: Literal["open", "closed"]
    resolution: str | None
    closed_at: datetime | None

    @field_serializer("closed_at", when_used="json")
    # 公開時間以 UTC 表示，避免資料庫連線時區影響回應。
    def utc_closed_at(self, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class TicketDetail(TicketSummary):
    question_id: UUID
    attempt_id: UUID
    answer_status: AnswerStatus | Literal["stopped"]
    answer: str | None
    citations: list[knowledge.SearchResult]
    search_results: list[knowledge.SearchResult]
    created_by: sessions.Role


class TicketPage(BaseModel):
    tickets: list[TicketSummary]
    has_more: bool


# 工單操作一律使用相同的 Session／啟用授權，不用路由快查代替交易內檢查。
def require_access(conn: TicketConnection, settings: Settings, digest: bytes) -> None:
    access = authorize(conn, settings, digest)
    if isinstance(access, AnswerDenied):
        raise access


@contextmanager
# 保留 Session 撤銷，並用儲存點讓任何拒絕都可回滾工單和問答異動。
def ticket_transaction(
    settings: Settings, digest: bytes
) -> Iterator[tuple[TicketConnection, sessions.Identity]]:
    with settings.connect() as conn:
        # 先有界等待授權鎖，保留受鎖 Session 提供的角色與帳號。
        bound_transaction(conn)
        access = authorize(conn, settings, digest)
        if isinstance(access, AnswerDenied):
            rejected = access
        else:
            try:
                # Session 與啟用鎖早於私人資料鎖；儲存點的回滾不會釋放這兩把鎖。
                with conn.transaction():
                    yield conn, access.identity
            except AnswerDenied as denied:
                if denied.code != 401:
                    raise
                # 若在私人 SQL 中等到登入到期，儲存點回滾後仍須提交撤銷。
                sessions.revoke_session(conn, digest)
                rejected = denied
            else:
                return
    raise rejected


# 只對已驗證的建立人找快照；原次或已核對的重送識別不依賴暫存。
def find_question_ticket(
    conn: TicketConnection,
    username: str,
    form: TicketInput,
    *,
    replay_only: bool,
) -> TicketDetail | None:
    with conn.cursor(row_factory=class_row(TicketDetail)) as cursor:
        return cursor.execute(
            """
            SELECT t.* FROM tickets t WHERE t.created_by = %s AND t.question_id = %s
                AND (NOT %s OR t.attempt_id = %s OR EXISTS (
                    SELECT 1 FROM ticket_attempt_replays r
                    WHERE r.ticket_id = t.id AND r.attempt_id = %s
                ))
            """,
            (username, form.question_id, replay_only, form.attempt_id, form.attempt_id),
        ).fetchone()


# 先鎖對話再鎖指定嘗試，與模型送出、重試、終態寫入及清理使用相同順序。
def capture_ticket(
    conn: TicketConnection,
    settings: Settings,
    digest: bytes,
    username: str,
    form: TicketInput,
) -> tuple[TicketDetail, datetime | None]:
    # 先查已核對的自有識別；原次及已受理的重送不須讀取已到期的問答。
    existing = find_question_ticket(conn, username, form, replay_only=True)
    if existing is not None:
        return existing, None
    source = conn.execute(
        """
        SELECT c.id FROM questions q JOIN conversations c ON c.id = q.conversation_id
        WHERE q.id = %s AND c.username = %s
        """,
        (form.question_id, username),
    ).fetchone()
    if source is None or not isinstance(source[0], UUID):
        # 初查後若另一筆已提交建單或重送識別，即使暫存清理也可讀回。
        require_access(conn, settings, digest)
        existing = find_question_ticket(conn, username, form, replay_only=True)
        if existing is not None:
            return existing, None
        raise AnswerDenied(404, NOT_FOUND)

    # 對話鎖會等待其他建單、重送或清理；取鎖後重查已提交的重送識別。
    window = lock_conversation(conn, source[0], username, update=True)
    require_access(conn, settings, digest)
    existing = find_question_ticket(conn, username, form, replay_only=True)
    if existing is not None:
        return existing, None
    if window is None:
        raise AnswerDenied(404, NOT_FOUND)
    expires = conversation_expiry(window[0], window[1])
    if sessions.utc_now() >= expires:
        raise AnswerDenied(410, EXPIRED)

    # 先只驗證識別及新舊，不在可能到期的鎖等待期間讀取證據。
    attempt = conn.execute(
        """
        SELECT a.status, NOT EXISTS (
            SELECT 1 FROM answer_attempts newer
            WHERE newer.question_id = a.question_id AND newer.attempt_number > a.attempt_number
        )
        FROM answer_attempts a WHERE a.id = %s AND a.question_id = %s FOR UPDATE
        """,
        (form.attempt_id, form.question_id),
    ).fetchone()
    require_access(conn, settings, digest)
    if sessions.utc_now() >= expires:
        raise AnswerDenied(410, EXPIRED)
    if attempt is None:
        raise AnswerDenied(404, NOT_FOUND)
    if not attempt[1]:
        raise AnswerDenied(409, STALE_ATTEMPT)
    existing = find_question_ticket(conn, username, form, replay_only=False)
    if existing is not None:
        # 僅登記已驗證的目前嘗試；若提交前失去授權，登記也會回滾。
        conn.execute(
            """
            INSERT INTO ticket_attempt_replays (ticket_id, attempt_id)
            VALUES (%s, %s) ON CONFLICT (ticket_id, attempt_id) DO NOTHING
            """,
            (existing.id, form.attempt_id),
        )
        return existing, expires

    # 停止等待與擷取已提交證據同交易生效；不觸碰帳務或其他問答。
    if attempt[0] == "running":
        conn.execute(
            "UPDATE answer_attempts SET status = 'stopped' WHERE id = %s",
            (form.attempt_id,),
        )
    with conn.cursor(row_factory=class_row(TicketDetail)) as cursor:
        saved = cursor.execute(
            """
            INSERT INTO tickets (id, question_id, attempt_id, description, question,
                answer_status, answer, citations, search_results, created_by, created_at)
            SELECT %s, q.id, a.id, %s, q.question, a.status, a.answer, a.citations,
                a.search_results, %s, %s
            FROM questions q JOIN answer_attempts a ON a.question_id = q.id
            WHERE q.id = %s AND a.id = %s
            RETURNING *
            """,
            (
                uuid4(),
                form.description,
                username,
                sessions.utc_now(),
                form.question_id,
                form.attempt_id,
            ),
        ).fetchone()
    if saved is None:
        raise AnswerDenied(404, NOT_FOUND)
    return saved, expires


# 保留原描述；首次建單和首次登記重送識別都在提交前重查授權及暫存期限。
def create_ticket(settings: Settings, digest: bytes, form: TicketInput) -> TicketDetail:
    with ticket_transaction(settings, digest) as (conn, identity):
        # 即使客服可跨帳號讀工單，建單與重送仍只處理登入帳號的問答。
        ticket, expires = capture_ticket(
            conn, settings, digest, identity.username, form
        )
        # 即使 INSERT 等過鎖，仍不能以已逾期的登入或來源提交並交付工單。
        require_access(conn, settings, digest)
        if expires is not None and sessions.utc_now() >= expires:
            raise AnswerDenied(410, EXPIRED)
        return ticket


# 詳情只讀工單快照，且無權和不存在共用同一拒絕回應。
def read_ticket(settings: Settings, digest: bytes, identifier: UUID) -> TicketDetail:
    with ticket_transaction(settings, digest) as (conn, identity):
        # 使用受鎖 Session 的角色決定是否開放查閱其他帳號的快照。
        with conn.cursor(row_factory=class_row(TicketDetail)) as cursor:
            ticket = cursor.execute(
                "SELECT * FROM tickets WHERE id = %s AND (%s OR created_by = %s)",
                (identifier, identity.role == "support", identity.username),
            ).fetchone()
        # 等待讀取期間到期就不交付私人資料。
        require_access(conn, settings, digest)
        if ticket is None:
            raise AnswerDenied(404, NOT_FOUND)
        return ticket


# 以穩定的兩欄排序分頁，多讀一筆判斷是否有下一頁，不掃描完整總數。
def list_tickets(
    settings: Settings, digest: bytes, limit: int, offset: int
) -> TicketPage:
    with ticket_transaction(settings, digest) as (conn, identity):
        # 客服可查全部摘要，一般帳號仍只在自己的工單範圍內分頁。
        with conn.cursor(row_factory=class_row(TicketSummary)) as cursor:
            rows = cursor.execute(
                """
                SELECT id, description, question, created_at, status, resolution, closed_at
                FROM tickets WHERE (%s OR created_by = %s)
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (identity.role == "support", identity.username, limit + 1, offset),
            ).fetchall()
        # 列表只交付含原始問題的摘要；讀取不會延長工作階段。
        require_access(conn, settings, digest)
        return TicketPage(tickets=rows[:limit], has_more=len(rows) > limit)


# 同一張工單只能結案一次，結案過程不讀取來源暫存或改寫快照。
def close_ticket(
    settings: Settings, digest: bytes, ticket_id: str, form: ResolutionInput
) -> TicketDetail:
    with ticket_transaction(settings, digest) as (conn, identity):
        # 先驗證受鎖 Session 的角色，避免一般帳號藉識別格式或查詢結果探測工單。
        if identity.role != "support":
            raise AnswerDenied(403, CLOSE_FORBIDDEN)
        try:
            identifier = parse_answer_id(ticket_id)
        except ValueError:
            raise AnswerDenied(404, NOT_FOUND) from None

        # 序列化結案但允許重送登記的外鍵鎖，等待時不讀取私人快照。
        current = conn.execute(
            "SELECT status FROM tickets WHERE id = %s FOR NO KEY UPDATE",
            (identifier,),
        ).fetchone()
        # 鎖等待後須重新確認授權，才能回覆工單是否存在或已結案。
        require_access(conn, settings, digest)
        if current is None:
            raise AnswerDenied(404, NOT_FOUND)
        if current[0] != "open":
            raise AnswerDenied(409, ALREADY_CLOSED)

        # 持鎖更新三個結案欄位，使用等到前一筆操作後的 UTC 時間。
        with conn.cursor(row_factory=class_row(TicketDetail)) as cursor:
            saved = cursor.execute(
                """
                UPDATE tickets SET status = 'closed', resolution = %s, closed_at = %s
                WHERE id = %s RETURNING *
                """,
                (form.resolution, sessions.utc_now(), identifier),
            ).fetchone()
        # 角色由同一受鎖 Session 決定；更新若等到失效，儲存點回滾後再交付拒絕。
        require_access(conn, settings, digest)
        if saved is None:
            raise AnswerDenied(404, NOT_FOUND)
        return saved
