from dataclasses import dataclass, field
from datetime import datetime
from os import environ
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.conninfo import make_conninfo

from backend.credentials import KEY_NAME, CredentialError, read_private
from backend.provider import Delivery

SCHEMA = Path(__file__).with_name("schema.sql")


@dataclass(frozen=True)
class Settings:
    key_file: Path
    database_dsn: str = field(repr=False)

    # 只從內部掛載取得資料庫密碼，讓 HTTP 與 CLI 共用同一個部署設定。
    @classmethod
    def from_environment(cls) -> "Settings":
        # 拒絕缺失的密碼，避免落入 libpq 的其他預設登入方式。
        password_path = Path(
            environ.get(
                "AI_SUPPORT_DB_PASSWORD_FILE", "/run/ai-support/database-password"
            )
        )
        password = read_private(password_path)
        if password is None or not password.strip():
            raise CredentialError("請先用 ./bin/local 初始化本機資料庫設定。")

        # 以具名參數傳給 libpq，避免設定值成為連線字串語法。
        return cls(
            Path(
                environ.get("AI_SUPPORT_KEY_FILE", f"/run/ai-support/gemini/{KEY_NAME}")
            ),
            make_conninfo(
                host=environ.get("AI_SUPPORT_DB_HOST", "db"),
                dbname="ai_support",
                user="ai_support",
                password=password.strip(),
            ),
        )

    # 有界等待資料庫連線，由呼叫者決定每次交易的生命週期。
    def connect(self) -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(self.database_dsn, connect_timeout=5)


# 多程序可同時啟動；交易鎖讓重跑 schema 時不相互競爭或覆寫既有狀態。
def initialize(settings: Settings) -> None:
    with settings.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7680281636712970521)")
        conn.execute(SCHEMA.read_text(encoding="utf-8"))


# 於外呼前一次原子保留臺灣日期的全服務額度，並新增預設送達不明的紀錄。
def reserve_request(
    conn: psycopg.Connection[tuple[object, ...]],
    request_id: UUID,
    *,
    at: datetime | None = None,
) -> bool:
    # upsert 的列鎖與條件在同一 SQL 生效；上限已滿時不插入請求紀錄。
    cursor = conn.execute(
        """
        WITH reservation AS (
            INSERT INTO daily_model_quota (service_day, used)
            VALUES ((COALESCE(%s::timestamptz, clock_timestamp()) AT TIME ZONE 'Asia/Taipei')::date, 1)
            ON CONFLICT (service_day) DO UPDATE SET used = daily_model_quota.used + 1
            WHERE daily_model_quota.used < 100
            RETURNING service_day
        )
        INSERT INTO model_requests (id, service_day)
        SELECT %s, service_day FROM reservation
        """,
        (at, request_id),
    )
    return cursor.rowcount == 1


# 一併記錄已送出與本機取消；送達不明沿用預留時的狀態及用量。
def settle_request(
    conn: psycopg.Connection[tuple[object, ...]], request_id: UUID, delivery: Delivery
) -> None:
    # 只在確定尚未送出時依原預留日期退回一次，跨午夜也不調整到新日期。
    if delivery == "unsent":
        conn.execute(
            """
            WITH cancelled AS (
                UPDATE model_requests SET delivery = 'unsent'
                WHERE id = %s AND delivery = 'unknown'
                RETURNING service_day
            )
            UPDATE daily_model_quota SET used = daily_model_quota.used - 1
            FROM cancelled WHERE daily_model_quota.service_day = cancelled.service_day
            """,
            (request_id,),
        )
    elif delivery == "sent":
        conn.execute(
            "UPDATE model_requests SET delivery = 'sent' WHERE id = %s AND delivery = 'unknown'",
            (request_id,),
        )
