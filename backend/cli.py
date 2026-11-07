import sys

import psycopg

from backend import knowledge
from backend.activation import verify_key
from backend.credentials import CredentialError
from backend.database import Settings, initialize


# 從 stdin 讀一批文件；完整驗證成功才交給資料庫替換。
def import_knowledge(settings: Settings) -> tuple[int, str]:
    # 多讀一個位元組即可辨識超限，格式與讀取錯誤都不顯示原文。
    try:
        batch = knowledge.parse_batch(sys.stdin.buffer.read(knowledge.IMPORT_LIMIT + 1))
    except (OSError, ValueError, RecursionError):
        return 1, "無法匯入知識文件：請檢查 JSON 格式、內容與大小。"

    # 執行中的後端已建立資料表；匯入只更新知識文件，不重跑初始化。
    knowledge.replace_documents(settings, batch)
    return 0, f"已匯入 {len(batch.documents)} 份知識文件。"


# 本機操作者可驗證 Key 或匯入文件；終端只接收安全訊息與退出狀態。
def main(args: list[str]) -> int:
    # 僅接受命令名稱，資料與 Key 都不作為程序參數。
    if args not in (["verify-key"], ["import-knowledge"]):
        print(
            "用法：python -m backend.cli {verify-key|import-knowledge}", file=sys.stderr
        )
        return 2

    try:
        # 沿用同一份部署設定；只有驗證 Key 沿用獨立初始化。
        settings = Settings.from_environment()
        if args == ["verify-key"]:
            initialize(settings)
            code, message = verify_key(settings)
        else:
            code, message = import_knowledge(settings)
    except CredentialError as error:
        code, message = 1, str(error)
    except psycopg.Error:
        if args == ["verify-key"]:
            code, message = 1, "無法讀寫本機資料庫。請確認服務狀態後再檢查啟用結果。"
        else:
            code, message = 1, "無法匯入知識文件。請確認本機服務與資料庫狀態。"

    # 回覆時保留可供 shell 判斷的退出碼，不顯示外部回覆或原始例外。
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
