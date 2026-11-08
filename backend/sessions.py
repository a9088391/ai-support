import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from math import ceil
from threading import BoundedSemaphore
from typing import Literal, cast

import psycopg
from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from pydantic import BaseModel, ConfigDict, Field

from backend.credentials import (
    DEMO_ACCOUNTS,
    CredentialError,
    read_demo_passwords,
)
from backend.database import Settings

Role = Literal["user", "support"]
COOKIE_NAME = "ai_support_session"
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
USERNAME_FORMAT = re.compile(r"[a-zA-Z0-9_-]{1,64}")
ATTEMPT_WINDOW = timedelta(minutes=15)
SESSION_IDLE = timedelta(minutes=30)
SESSION_MAX = timedelta(hours=8)
PASSWORD_WORK = BoundedSemaphore(4)


class LoginInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128, repr=False)


class Identity(BaseModel):
    username: str
    role: Role


class LoginLimited(Exception):
    # 保存剩餘秒數供 HTTP Retry-After 使用，避免透露哪個限制生效。
    def __init__(self, retry_after: int) -> None:
        super().__init__()
        self.retry_after = retry_after


# 取得單次操作的 UTC 時間，期限判斷與同次寫入共用此值。
def utc_now() -> datetime:
    return datetime.now(UTC)


# 應用程式交由既有 Argon2id 實作比對，只有憑證不符轉為一般失敗。
def password_matches(password: str, encoded: str) -> bool:
    try:
        # 避免多個本機請求同時保留 Argon2id 工作記憶體。
        with PASSWORD_WORK:
            Argon2id.verify_phc_encoded(password.encode("utf-8"), encoded)
    except InvalidKey:
        return False
    return True


# 資料庫可能已有啟用紀錄；帳號獨立初始化，多個啟動者只能寫入一次。
def initialize_accounts(settings: Settings) -> None:
    passwords = read_demo_passwords(settings.demo_passwords_file)
    with settings.connect() as conn:
        # 同一把交易鎖涵蓋比對及兩筆新增，避免並行啟動產生部分帳號。
        conn.execute("SELECT pg_advisory_xact_lock(7680281636712970522)")
        stored = {
            row[0]: row[1]
            for row in conn.execute("SELECT username, password_hash FROM demo_accounts")
        }
        if stored:
            # 保留既有資料及工作階段；檔案遺失後重新產生的密碼不得冒充原檔。
            for username, password in zip(DEMO_ACCOUNTS, passwords, strict=True):
                encoded = stored.get(username)
                if not isinstance(encoded, str) or not password_matches(
                    password, encoded
                ):
                    raise CredentialError(
                        "示範帳號密碼檔與資料庫不符，請復原原始私有檔案。"
                    )
            return

        # 每個帳號獨立加鹽，將參數與摘要保存為 PHC 格式。
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO demo_accounts (username, password_hash) VALUES (%s, %s)",
                (
                    (
                        username,
                        Argon2id(
                            salt=secrets.token_bytes(16),
                            length=32,
                            iterations=2,
                            lanes=1,
                            memory_cost=19 * 1024,
                        ).derive_phc_encoded(password.encode("ascii")),
                    )
                    for username, password in zip(DEMO_ACCOUNTS, passwords, strict=True)
                ),
            )


# 只接受服務簽發的 Cookie 形狀，資料庫全程只使用其摘要。
def token_digest(token: str | None) -> bytes | None:
    if token is None or TOKEN_FORMAT.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).digest()


# 依賴登入限制交易已持鎖，在固定時間窗內保留一次嘗試或回傳等待時間。
def consume_attempt(
    conn: psycopg.Connection[tuple[object, ...]],
    dimension: str,
    bucket: str,
    limit: int,
    at: datetime,
) -> int:
    # 呼叫端已清除到期窗；達上限時保留原期限與計數。
    accepted = conn.execute(
        """
        INSERT INTO login_rate_limits (dimension, bucket, attempts, expires_at)
        VALUES (%s, %s, 1, %s)
        ON CONFLICT (dimension, bucket) DO UPDATE SET attempts = login_rate_limits.attempts + 1
        WHERE login_rate_limits.attempts < %s
        """,
        (dimension, bucket, at + ATTEMPT_WINDOW, limit),
    )
    if accepted.rowcount == 1:
        return 0

    # 同一交易讀取本次未更新的時間窗，向上取整避免提早解鎖。
    window = conn.execute(
        "SELECT expires_at FROM login_rate_limits WHERE dimension = %s AND bucket = %s",
        (dimension, bucket),
    ).fetchone()
    if window is None or not isinstance(window[0], datetime):
        raise psycopg.DatabaseError("缺少登入限制時間窗。")
    return max(1, ceil((window[0] - at).total_seconds()))


# 登入先提交限流紀錄再做雜湊，並在成功時原子撤銷舊 Cookie 並換發新值。
def login(
    settings: Settings, username: str, password: str, source: str, previous: str | None
) -> tuple[Identity, str] | None:
    known = username in DEMO_ACCOUNTS
    with settings.connect() as conn:
        # 短交易序列化兩個限制；同一時間點判斷過期及本次額度。
        conn.execute("SELECT pg_advisory_xact_lock(7680281636712970523)")
        at = utc_now()
        conn.execute("DELETE FROM login_rate_limits WHERE expires_at <= %s", (at,))
        retry = consume_attempt(conn, "source", source, 20, at)
        if not retry:
            # 來源受限時不耗帳號額度；所有名稱以同一摘要規則建立獨立計數。
            account = hashlib.sha256(username.encode("ascii")).hexdigest()
            retry = consume_attempt(conn, "account", account, 5, at)

        # 未知帳號仍比對既有 Argon2id 雜湊，以降低有效帳號可被時序辨別的機會。
        row = None
        if not retry:
            row = conn.execute(
                "SELECT password_hash FROM demo_accounts WHERE username = %s",
                (username if known else DEMO_ACCOUNTS[0],),
            ).fetchone()

    # 拒絕也保留已分配的來源額度；雜湊期間沒有持有資料庫交易或鎖。
    if retry:
        raise LoginLimited(retry)
    if row is None or not isinstance(row[0], str):
        raise psycopg.DatabaseError("缺少示範帳號。")
    if not password_matches(password, row[0]) or not known:
        return None

    # 先撤銷本次 Cookie，之後清除到期列只略過已鎖定的資料，避免交叉等待。
    token = secrets.token_urlsafe(32)
    with settings.connect() as conn:
        revoke_session(conn, token_digest(previous))
        conn.execute(
            """
            DELETE FROM login_sessions WHERE token_digest IN (
                SELECT token_digest FROM login_sessions
                WHERE created_at <= %s OR last_seen_at <= %s
                FOR UPDATE SKIP LOCKED
            )
            """,
            (at - SESSION_MAX, at - SESSION_IDLE),
        )
        conn.execute(
            """
            INSERT INTO login_sessions (token_digest, username, created_at, last_seen_at)
            VALUES (%s, %s, %s, %s)
            """,
            (token_digest(token), username, at, at),
        )
    return Identity(username=username, role=cast(Role, username)), token


# 工作階段入口自管交易；其他業務可在自己的交易內共用列鎖驗證。
def active_session(
    settings: Settings, token: str | None, *, refresh: bool
) -> Identity | None:
    # 不符合憑證格式時直接拒絕，避免不必要的資料庫查詢。
    digest = token_digest(token)
    if digest is None:
        return None

    # 離開交易時提交刷新或刪除，包括找不到有效身分的情況。
    with settings.connect() as conn:
        return read_active_session(conn, digest, refresh=refresh)


# 既有呼叫端只需公開身分；登入期限不進 HTTP 回應。
def read_active_session(
    conn: psycopg.Connection[tuple[object, ...]], digest: bytes, *, refresh: bool
) -> Identity | None:
    session = read_locked_session(conn, digest, refresh=refresh)
    return session[0] if session is not None else None


# 呼叫端持有同一列鎖，並取得不含秘密的到期時間供送出前再次比對。
def read_locked_session(
    conn: psycopg.Connection[tuple[object, ...]], digest: bytes, *, refresh: bool
) -> tuple[Identity, datetime] | None:
    # 先取得列鎖，避免用等待期間已經過時的時間接受逾期工作階段。
    row = conn.execute(
        """
        SELECT username, created_at, last_seen_at FROM login_sessions
        WHERE token_digest = %s FOR UPDATE
        """,
        (digest,),
    ).fetchone()
    if row is None:
        return None

    # 共用期限；首次觀察到期即刪除，避免時鐘回撥恢復憑證。
    username, created_at, last_seen_at = cast(tuple[Role, datetime, datetime], row)
    at = utc_now()
    absolute_expiry = created_at + SESSION_MAX
    if at >= min(last_seen_at + SESSION_IDLE, absolute_expiry):
        revoke_session(conn, digest)
        return None

    # 只有主動刷新能推進活動時間；仍持有列鎖，且時鐘回撥時不倒退。
    if refresh and at > last_seen_at:
        conn.execute(
            "UPDATE login_sessions SET last_seen_at = %s WHERE token_digest = %s",
            (at, digest),
        )
        last_seen_at = at
    return Identity(username=username, role=username), min(
        last_seen_at + SESSION_IDLE, absolute_expiry
    )


# 登出與換發共用刪除操作；不存在的憑證可安全重送。
def revoke_session(
    conn: psycopg.Connection[tuple[object, ...]], digest: bytes | None
) -> None:
    if digest is not None:
        conn.execute("DELETE FROM login_sessions WHERE token_digest = %s", (digest,))


# 沒有可撤銷的憑證時直接完成，且不依賴啟用狀態。
def logout(settings: Settings, token: str | None) -> None:
    digest = token_digest(token)
    if digest is not None:
        with settings.connect() as conn:
            revoke_session(conn, digest)
