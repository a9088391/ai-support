from typing import Literal, cast
from uuid import uuid4

from psycopg import Connection, DatabaseError
from pydantic import BaseModel

from backend.credentials import CredentialError, GeminiCredential, read_key
from backend.database import Settings, reserve_request, settle_request
from backend.provider import MODEL, PROVIDER, ProbeOutcome, request_verification

SetupState = Literal["unconfigured", "unverified", "ready", "invalid"]
MESSAGES: dict[SetupState, str] = {
    "unconfigured": "尚未設定 Gemini API Key。請由本機操作人員完成設定。",
    "unverified": "目前的 Gemini 設定尚未通過驗證。請由本機操作人員執行驗證。",
    "ready": "目前的 Gemini 設定已通過驗證。",
    "invalid": "Google 已拒絕目前的憑證。請由本機操作人員更換 Key 並重新驗證。",
}
ACTIVATION_ROW = """
    SELECT a.status, a.key_version, a.fingerprint, a.provider, a.model, a.verified_sequence,
           EXISTS (
               SELECT 1 FROM model_requests AS r
               WHERE r.rejected_key_version = a.key_version
                   AND r.rejected_verified_sequence = a.verified_sequence
           ) AS rejected
    FROM activation AS a WHERE a.singleton
"""


class SetupResponse(BaseModel):
    status: SetupState
    message: str
    model: str = MODEL


# 依每次讀到的憑證比對驗證紀錄，只回傳安全的公開狀態。
def setup_status(
    settings: Settings, *, lock_with: Connection[tuple[object, ...]] | None = None
) -> SetupResponse:
    status, _, _ = activation_binding(settings, lock_with=lock_with)
    return status


# 共用同一組啟用規則，業務外呼額外取得經驗證的憑證及更新順序。
def activation_binding(
    settings: Settings, *, lock_with: Connection[tuple[object, ...]] | None = None
) -> tuple[SetupResponse, GeminiCredential | None, int]:
    # 業務讀取要先取得共用列鎖再讀 Key，以免等待驗證寫入時沿用舊設定。
    if lock_with is not None:
        lock_with.execute("SELECT singleton FROM activation WHERE singleton FOR SHARE")

    # 缺少憑證或檔案有誤時直接關閉業務入口。
    try:
        key = read_key(settings.key_file)
    except CredentialError:
        return (
            SetupResponse(
                status="unverified",
                message="本機 Key 設定無法讀取。請由本機操作人員檢查設定。",
            ),
            None,
            0,
        )
    if key is None:
        return (
            SetupResponse(status="unconfigured", message=MESSAGES["unconfigured"]),
            None,
            0,
        )

    # 鎖可能等待已久；取得鎖後以新語句讀取啟用與已提交的拒絕，且共用同一快照。
    if lock_with is not None:
        row = lock_with.execute(ACTIVATION_ROW).fetchone()
    else:
        # 入口查詢不用等列鎖；設定及帳本仍須由單一語句取得一致快照。
        with settings.connect() as conn:
            row = conn.execute(ACTIVATION_ROW).fetchone()

    # 只接受與目前設定相符的驗證；相同驗證序號被拒絕時必須重新明確驗證。
    status: SetupState = "unverified"
    if row is not None and row[1:5] == (key.version, key.fingerprint, PROVIDER, MODEL):
        if row[0] == "invalid" or row[6] is True:
            status = "invalid"
        elif row[0] == "ready":
            return (
                SetupResponse(status="ready", message=MESSAGES["ready"]),
                key,
                cast(int, row[5]),
            )
    return SetupResponse(status=status, message=MESSAGES[status]), None, 0


# 只有本機 CLI 呼叫此流程，將用量預留、唯一外呼與結果寫回切成明確邊界。
def verify_key(settings: Settings) -> tuple[int, str]:
    request_id = uuid4()

    # 先鎖單例，再讀設定並預留；紀錄開始順序，外呼期間不持鎖。
    with settings.connect() as conn:
        conn.execute("SELECT singleton FROM activation WHERE singleton FOR UPDATE")
        key = read_key(settings.key_file)
        if key is None:
            return 1, MESSAGES["unconfigured"]
        reserve_request(conn, request_id)
        binding = (key.version, key.fingerprint, PROVIDER, MODEL)
        sequence = conn.execute(
            """
            UPDATE activation SET
                status = CASE
                    WHEN (key_version, fingerprint, provider, model) = (%s, %s, %s, %s)
                    THEN status ELSE 'unverified'
                END,
                key_version = %s, fingerprint = %s, provider = %s, model = %s,
                probe_sequence = probe_sequence + 1
            WHERE singleton
            RETURNING probe_sequence
            """,
            (*binding, *binding),
        ).fetchone()
        if sequence is None:
            raise DatabaseError("缺少啟用紀錄。")

    # 確認目前設定，檔案移除或讀取失敗也視為舊憑證無法使用。
    def current_key() -> GeminiCredential | None:
        try:
            return read_key(settings.key_file)
        except CredentialError:
            return None

    # DNS 和 TLS 完成後，送出 HTTP 前再核對 Key；用量仍歸入預留日期。
    def before_send() -> bool:
        return current_key() == key

    # 連線前已知設定變更就取消，避免未寫入的請求被連線故障誤列為送達不明。
    outcome = (
        request_verification(key.value, before_send)
        if current_key() == key
        else ProbeOutcome(None, "unsent")
    )

    # 無論設定是否切換都結算請求；同設定的明確結果才有資格更新啟用紀錄。
    with settings.connect() as conn:
        settle_request(conn, request_id, outcome.delivery)
        if current_key() != key:
            return 1, "驗證期間設定已變更。請確認目前設定後重新驗證。"
        if outcome.delivery == "unsent":
            return 1, "模型請求未送出。請確認設定後重新驗證。"

        # 供應商限流已送出且入帳，既有驗證狀態不受影響。
        if outcome.status == "rate_limited":
            return 1, "Gemini 限流（429）。請稍後重新驗證。"
        if outcome.status is not None:
            # 只比較已套用的明確結果；較新的暫時錯誤不會蓋掉較舊的明確無效。
            applied = conn.execute(
                """
                UPDATE activation SET status = %s, verified_sequence = %s
                WHERE singleton AND (key_version, fingerprint, provider, model) = (%s, %s, %s, %s)
                    AND verified_sequence < %s
                """,
                (outcome.status, sequence[0], *binding, sequence[0]),
            )
            if applied.rowcount == 1:
                return (0 if outcome.status == "ready" else 1), MESSAGES[outcome.status]
            return 1, "驗證結果已由較新的操作接手。"

    # 服務暫時失敗或回覆不完整時，不修改現有憑證的驗證狀態。
    return 1, "Gemini 未能完成本次驗證。請檢查供應商狀態或配額後再試。"
