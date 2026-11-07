from typing import Literal
from uuid import uuid4

from psycopg import DatabaseError
from pydantic import BaseModel

from backend.credentials import CredentialError, read_key
from backend.database import Settings, reserve_request, settle_request
from backend.provider import MODEL, PROVIDER, ProbeOutcome, request_verification

SetupState = Literal["unconfigured", "unverified", "ready", "invalid"]
MESSAGES: dict[SetupState, str] = {
    "unconfigured": "尚未設定 Gemini API Key。請由本機操作人員完成設定。",
    "unverified": "目前的 Gemini 設定尚未通過驗證。請由本機操作人員執行驗證。",
    "ready": "目前的 Gemini 設定已通過驗證。",
    "invalid": "Google 已拒絕目前的憑證。請由本機操作人員更換 Key 並重新驗證。",
}


class SetupResponse(BaseModel):
    status: SetupState
    message: str
    model: str = MODEL


# 依每次讀到的憑證比對驗證紀錄，只回傳安全的公開狀態。
def setup_status(settings: Settings) -> SetupResponse:
    # 缺少憑證或檔案有誤時直接關閉業務入口。
    try:
        key = read_key(settings.key_file)
    except CredentialError:
        return SetupResponse(
            status="unverified",
            message="本機 Key 設定無法讀取。請由本機操作人員檢查設定。",
        )
    if key is None:
        return SetupResponse(status="unconfigured", message=MESSAGES["unconfigured"])

    # 只有當版本、Key 指紋、供應商與模型全部吻合，才沿用之前的驗證結果。
    with settings.connect() as conn:
        row = conn.execute(
            """
            SELECT status FROM activation
            WHERE singleton AND key_version = %s AND fingerprint = %s
                AND provider = %s AND model = %s
            """,
            (key.version, key.fingerprint, PROVIDER, MODEL),
        ).fetchone()

    # 以已知狀態產生 DTO，未知紀錄不放行。
    status: SetupState = "unverified"
    if row and row[0] == "ready":
        status = "ready"
    elif row and row[0] == "invalid":
        status = "invalid"
    return SetupResponse(status=status, message=MESSAGES[status])


# 只有本機 CLI 呼叫此流程，將額度預留、唯一外呼與結果寫回切成明確邊界。
def verify_key(settings: Settings) -> tuple[int, str]:
    request_id = uuid4()

    # 先鎖單例，再讀設定並預留；紀錄開始順序，外呼期間不持鎖。
    with settings.connect() as conn:
        conn.execute("SELECT singleton FROM activation WHERE singleton FOR UPDATE")
        key = read_key(settings.key_file)
        if key is None:
            return 1, MESSAGES["unconfigured"]
        if not reserve_request(conn, request_id):
            return 1, "今日全服務模型額度已用完，將於臺灣時間午夜恢復。"
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

    # 已切換或移除的 Key 在送出前可取消；此時確定沒有消耗模型請求。
    try:
        current = read_key(settings.key_file)
    except CredentialError:
        current = None
    outcome = (
        request_verification(key.value)
        if current == key
        else ProbeOutcome(None, "unsent")
    )

    # 無論設定是否切換都結算請求；同設定的明確結果才有資格更新啟用紀錄。
    with settings.connect() as conn:
        settle_request(conn, request_id, outcome.delivery)
        try:
            current = read_key(settings.key_file)
        except CredentialError:
            current = None
        if current != key:
            return 1, "驗證期間設定已變更。請確認目前設定後重新驗證。"
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
