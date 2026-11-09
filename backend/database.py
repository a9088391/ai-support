from dataclasses import dataclass, field
from datetime import date, datetime
from os import environ
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from backend.credentials import (
    DEMO_PASSWORDS_NAME,
    KEY_NAME,
    CredentialError,
    read_private,
)
from backend.provider import Delivery

SCHEMA = Path(__file__).with_name("schema.sql")


@dataclass(frozen=True)
class Settings:
    key_file: Path
    database_dsn: str = field(repr=False)
    demo_passwords_file: Path

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
            Path(
                environ.get(
                    "AI_SUPPORT_DEMO_PASSWORDS_FILE",
                    f"/run/ai-support/{DEMO_PASSWORDS_NAME}",
                )
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


# 於外呼前原子登記臺灣日期用量及預設送達不明的紀錄。
def reserve_request(
    conn: psycopg.Connection[tuple[object, ...]],
    request_id: UUID,
    *,
    at: datetime | None = None,
    username: str | None = None,
) -> date:
    # 預留及取消共用短交易鎖；取鎖後以資料庫時間決定用量歸屬。
    conn.execute("SELECT pg_advisory_xact_lock(7680281636712970524)")
    cursor = conn.execute(
        """
        WITH reservation AS (
            INSERT INTO daily_model_quota (service_day, used)
            VALUES ((COALESCE(%s::timestamptz, clock_timestamp()) AT TIME ZONE 'Asia/Taipei')::date, 1)
            ON CONFLICT (service_day) DO UPDATE SET used = daily_model_quota.used + 1
            RETURNING service_day
        )
        INSERT INTO model_requests (id, service_day, username)
        SELECT %s, service_day, %s FROM reservation
        RETURNING service_day
        """,
        (at, request_id, username),
    )
    # 同一語句必須寫入明細；缺少結果時回滾交易，避免未入帳就外呼。
    row = cursor.fetchone()
    if row is None or not isinstance(row[0], date):
        raise psycopg.DatabaseError("無法記錄模型請求用量。")
    return row[0]


# 一併記錄已送出與本機取消；送達不明沿用預留時的狀態及用量。
def settle_request(
    conn: psycopg.Connection[tuple[object, ...]], request_id: UUID, delivery: Delivery
) -> None:
    # 只在確定尚未送出時依原預留日期退回一次，跨午夜也不調整到新日期。
    if delivery == "unsent":
        conn.execute("SELECT pg_advisory_xact_lock(7680281636712970524)")
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


# 送出與拒絕共用同一驗證綁定的交易鎖；使用雙整數空間，與單整數的配額鎖分離。
def lock_request_binding(
    conn: psycopg.Connection[tuple[object, ...]],
    key_version: UUID,
    verified_sequence: int,
    *,
    shared: bool,
) -> None:
    # 雜湊若碰撞只會讓兩個綁定彼此等待，拒絕判斷仍用完整版本及驗證序號。
    operation = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    query = sql.SQL(
        "SELECT {}(768028164, hashtext(%s::uuid::text || ':' || %s::bigint::text))"
    ).format(sql.Identifier(operation))
    conn.execute(query, (key_version, verified_sequence))


# 同一筆帳本寫入確認送達及拒絕；不等啟用列鎖，也不改寫既有拒絕或當日額度。
def record_request_rejection(
    conn: psycopg.Connection[tuple[object, ...]],
    request_id: UUID,
    key_version: UUID,
    verified_sequence: int,
) -> None:
    updated = conn.execute(
        """
        UPDATE model_requests
        SET delivery = 'sent', rejected_key_version = %s, rejected_verified_sequence = %s
        WHERE id = %s AND username IS NOT NULL AND delivery IN ('unknown', 'sent')
            AND rejected_key_version IS NULL
        """,
        (key_version, verified_sequence, request_id),
    )
    if updated.rowcount != 1:
        raise psycopg.DatabaseError("無法記錄模型憑證拒絕。")
