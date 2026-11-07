import fcntl
import getpass
import hashlib
import os
import re
import secrets
import stat
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4

LOCAL_DIR = Path(".local")
KEY_NAME = "api-key"
DEMO_PASSWORDS_NAME = "demo-passwords"
DEMO_ACCOUNTS = ("user", "support")
DEMO_PASSWORDS_FORMAT = re.compile(
    r"user=([A-Za-z0-9_-]{43})\nsupport=([A-Za-z0-9_-]{43})\n"
)
# 新版 AQ. 驗證金鑰含點號；讀寫共用相同的 ASCII 字元與長度限制。
KEY_FORMAT = re.compile(r"[A-Za-z0-9_.-]{1,256}")


class CredentialError(Exception):
    pass


class CredentialFormatError(CredentialError):
    pass


@dataclass(frozen=True)
class GeminiCredential:
    version: UUID
    value: str = field(repr=False)

    # 資料庫只持久化指紋；版本使換回相同 Key 時也須重新驗證。
    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.value.encode("ascii")).hexdigest()


# 只從有適當權限的普通檔案讀取有限長度，避免解析或錯誤回傳秘密。
def read_private(path: Path) -> str | None:
    # 防止跟隨最末層符號連結，並在同一個 fd 驗證檔案型態及權限。
    try:
        with os.fdopen(
            os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK),
            encoding="ascii",
        ) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.geteuid()
            ):
                raise CredentialError(
                    "本機設定檔必須由操作使用者持有，且僅限擁有者讀寫。"
                )
            value = stream.read(1025)
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        # 只有通過檔案安全檢查後的解碼錯誤可視為內容損毀。
        raise CredentialFormatError("無法讀取受保護的本機設定檔。") from None
    except OSError:
        raise CredentialError("無法讀取受保護的本機設定檔。") from None

    # 拒絕異常長度，避免把不受控內容帶入 HTTP 標頭或資料庫。
    if len(value) > 1024:
        raise CredentialFormatError("本機設定檔格式不正確。")
    return value


# 解析原子更新的 Key 與設定版本，拒絕不完整或被手動破壞的設定。
def read_key(path: Path) -> GeminiCredential | None:
    # 檔案不存在是正常的未設定狀態。
    content = read_private(path)
    if content is None:
        return None

    # 檢查格式時只保留固定錯誤訊息。
    try:
        version, key = content.splitlines()
        if KEY_FORMAT.fullmatch(key) is None:
            raise ValueError
        return GeminiCredential(UUID(version), key)
    except ValueError:
        raise CredentialFormatError("本機 Key 格式不正確，請重新設定。") from None


# 示範密碼固定由本機初始化產生，檔案有誤時提供安全診斷。
def read_demo_passwords(path: Path) -> tuple[str, str]:
    content = read_private(path)
    match = DEMO_PASSWORDS_FORMAT.fullmatch(content) if content is not None else None
    if match is None or match[1] == match[2]:
        raise CredentialError("找不到可用的示範帳號密碼檔，請檢查本機設定。")
    return match[1], match[2]


# 依呼叫者取得的檔案狀態驗證目錄型態、擁有者與權限。
def require_private_directory(info: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid != os.geteuid()
    ):
        raise CredentialError("本機設定目錄必須由操作使用者持有，權限為 700。")


# 建立私有目錄後檢查權限及擁有者；lstat 也能拒絕既有符號連結。
def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    require_private_directory(path.lstat())


# 寫完整檔案才公布新設定；初始化可採排他建立，避免覆寫已用於 PostgreSQL 的密碼。
def write_private(path: Path, content: str, *, replace: bool) -> None:
    # tempfile 以 0600 建立，和目標同目錄才能原子切換。
    with NamedTemporaryFile(
        mode="w", encoding="ascii", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            if replace:
                temporary.replace(path)
            else:
                try:
                    path.hardlink_to(temporary)
                except FileExistsError:
                    pass
        finally:
            temporary.unlink(missing_ok=True)


# 在接觸檔案前檢查輸入；同一組 Key 保留現有版本，換 Key 則產生新版本。
def set_key(path: Path, key: str) -> None:
    # 拒絕控制字元、空值及過長內容，避免破壞私有檔案及 HTTP 標頭。
    if not key:
        raise CredentialError("Key 不可為空。")
    if KEY_FORMAT.fullmatch(key) is None:
        raise CredentialError(
            "Key 格式不正確。請從 Google AI Studio 複製完整的 API Key。"
        )

    # 鎖定原子換檔時仍維持同一 inode 的私有目錄，序列化讀取、比較與替換。
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        require_private_directory(os.fstat(directory_fd))
        fcntl.flock(directory_fd, fcntl.LOCK_EX)

        # 只允許復原已通過檔案安全檢查的內容損毀；其他錯誤維持拒絕。
        try:
            current = read_key(path)
        except CredentialFormatError:
            current = None

        # 相同憑證保留版本；替換或復原時產生新版本，要求重新驗證。
        if current is None or not secrets.compare_digest(current.value, key):
            write_private(path, f"{uuid4()}\n{key}\n", replace=True)
    finally:
        # 離開讀寫區間即關閉描述符並釋放鎖。
        os.close(directory_fd)


# 本機操作入口以無回顯終端讀取 Key，初始化密碼時保留既有檔案。
def main(args: list[str]) -> int:
    # 驗證命令後再建立本案專屬目錄或寫入憑證。
    if args not in (["prepare"], ["set-key"]):
        print("用法：python3 -m backend.credentials {prepare|set-key}", file=sys.stderr)
        return 2

    try:
        # 啟動與設定共用目錄檢查；準備只建立不存在的密碼檔。
        ensure_private_directory(LOCAL_DIR)
        key_dir = LOCAL_DIR / "gemini"
        ensure_private_directory(key_dir)
        if args == ["prepare"]:
            db_file = LOCAL_DIR / "database-password"
            if read_private(db_file) is None:
                write_private(db_file, secrets.token_urlsafe(32) + "\n", replace=False)

            # 分別取樣後以單檔排他公布，避免並行啟動或中斷留下半組密碼。
            demo_file = LOCAL_DIR / DEMO_PASSWORDS_NAME
            if read_private(demo_file) is None:
                write_private(
                    demo_file,
                    "".join(
                        f"{account}={secrets.token_urlsafe(32)}\n"
                        for account in DEMO_ACCOUNTS
                    ),
                    replace=False,
                )
            read_demo_passwords(demo_file)
            return 0

        # 無法關閉回顯就終止，避免 Key 從管線、命令列或可見終端輸入。
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Gemini API Key：")
        set_key(key_dir / KEY_NAME, key)
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        print("設定已取消。請在可關閉回顯的互動終端輸入 Key。", file=sys.stderr)
        return 1
    except CredentialError as error:
        print(error, file=sys.stderr)
        return 1
    except OSError:
        print("無法更新本機設定檔。", file=sys.stderr)
        return 1

    # 回覆完成後由操作者另外觸發驗證。
    print("Key 已儲存。請執行 ./bin/local verify-key。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
