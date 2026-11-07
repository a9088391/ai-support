import sys

import psycopg

from backend.activation import verify_key
from backend.credentials import CredentialError
from backend.database import Settings, initialize


# 由本機操作者明確觸發單次驗證，終端只接收安全訊息與退出狀態。
def main(args: list[str]) -> int:
    # 只接受既有命令，避免把 Key 當成程序參數。
    if args != ["verify-key"]:
        print("用法：python -m backend.cli verify-key", file=sys.stderr)
        return 2

    try:
        # CLI 可獨立於 HTTP 程序啟動，沿用同一份初始化與部署設定。
        settings = Settings.from_environment()
        initialize(settings)
        code, message = verify_key(settings)
    except CredentialError as error:
        code, message = 1, str(error)
    except psycopg.Error:
        code, message = 1, "無法讀寫本機資料庫。請確認服務狀態後再檢查啟用結果。"

    # 回覆時保留可供 shell 判斷的退出碼，不顯示外部回覆或原始例外。
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
