import getpass
import stat
from pathlib import Path

import pytest
from backend import credentials
from backend.credentials import CredentialError, read_demo_passwords, read_key


@pytest.mark.parametrize(
    "valid_key",
    [
        pytest.param("fixture-secret", id="legacy"),
        pytest.param("AQ.fixture-only-not-a-real-key", id="authentication"),
    ],
)
# 以實際本機命令入口檢查不同 Key 格式的儲存、重讀及錯誤後的檔案保留。
def test_local_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    valid_key: str,
) -> None:
    monkeypatch.chdir(tmp_path)

    # 初始化必須真的建立私有資料庫密碼，重跑則保留相同內容。
    assert credentials.main(["prepare"]) == 0
    local = Path(".local")
    password_path = local / "database-password"
    password = password_path.read_bytes()
    demo_file = local / "demo-passwords"
    demo_passwords = read_demo_passwords(demo_file)
    demo_inode = demo_file.stat().st_ino
    assert password.strip()
    assert stat.S_IMODE(local.stat().st_mode) == 0o700
    assert stat.S_IMODE(password_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(demo_file.stat().st_mode) == 0o600
    assert all(len(value) == 43 for value in demo_passwords)
    assert demo_passwords[0] != demo_passwords[1]
    assert credentials.main(["prepare"]) == 0
    assert password_path.read_bytes() == password
    assert demo_file.stat().st_ino == demo_inode
    assert read_demo_passwords(demo_file) == demo_passwords
    output = capsys.readouterr()
    assert all(value not in output.out + output.err for value in demo_passwords)

    # 模擬本機隱藏輸入，確認儲存後只有擁有者可讀寫。
    terminal_input = valid_key

    # 每次操作取得當時的輸入，重現成功設定後再送出無效內容。
    def entered_key(_: str) -> str:
        return terminal_input

    monkeypatch.setattr(getpass, "getpass", entered_key)
    assert credentials.main(["set-key"]) == 0
    key_path = local / "gemini" / "api-key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    key = read_key(key_path)
    assert key is not None
    assert key.value == valid_key
    assert valid_key not in repr(key)
    assert credentials.main(["set-key"]) == 0
    assert read_key(key_path) == key
    output = capsys.readouterr()
    assert valid_key not in output.out + output.err

    # 不安全或格式錯誤的操作不應覆寫既有 Key，也不可將輸入回顯。
    for terminal_input in (
        "",
        f" {valid_key}",
        f"{valid_key}\r\nunsafe",
        f"{valid_key}\x00",
        f"{valid_key}臺",
        f"{valid_key}{'x' * (257 - len(valid_key))}",
    ):
        assert credentials.main(["set-key"]) == 1
        assert read_key(key_path) == key
        output = capsys.readouterr()
        assert (
            "Key 不可為空。" if not terminal_input else "Key 格式不正確。"
        ) in output.err
        assert valid_key not in output.out + output.err

    # 命令列參數也不能提交憑證，錯誤輸出只顯示操作用法。
    assert credentials.main(["set-key", valid_key]) == 2
    assert read_key(key_path) == key
    output = capsys.readouterr()
    assert valid_key not in output.out + output.err


# 既有示範密碼檔無法安全讀取時，初始化不得自動覆寫或回顯內容。
def test_demo_passwords_preserve_invalid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert credentials.main(["prepare"]) == 0
    demo_file = Path(".local/demo-passwords")
    demo_file.write_text("fixture-malformed-password\n")
    assert credentials.main(["prepare"]) == 1
    assert demo_file.read_text() == "fixture-malformed-password\n"
    assert "fixture-malformed-password" not in capsys.readouterr().err


# 讀取範圍限擁有者的普通檔案，錯誤訊息不揭露檔案內容。
def test_insecure_key(tmp_path: Path) -> None:
    # 先建立權限過寬的普通檔案，再以連結嘗試指向它。
    target = tmp_path / "private"
    target.write_text("fixture-secret")
    target.chmod(0o644)
    link = tmp_path / "api-key"
    link.symlink_to(target)

    # 兩種不安全檔案都拒絕，錯誤訊息保持通用。
    for path in (target, link):
        with pytest.raises(CredentialError) as captured:
            read_key(path)
        assert "fixture-secret" not in str(captured.value)
        with pytest.raises(CredentialError):
            credentials.set_key(path, "fixture-new")
    assert link.is_symlink()
    assert target.read_text() == "fixture-secret"


@pytest.mark.parametrize(
    "broken_content",
    [
        b"invalid-version\nfixture-old\n",
        b"missing-key",
        b"\xff",
        b"x" * 1025,
    ],
    ids=["uuid", "line-count", "encoding", "length"],
)
# 安全檔案的內容損毀時，本機設定命令可用合法輸入復原。
def test_replace_malformed_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broken_content: bytes,
) -> None:
    # 準備受保護但損毀的 Key 檔，並確認目前無法讀作有效憑證。
    monkeypatch.chdir(tmp_path)
    assert credentials.main(["prepare"]) == 0
    path = Path(".local/gemini/api-key")
    path.touch(mode=0o600)
    path.write_bytes(broken_content)
    with pytest.raises(CredentialError):
        read_key(path)

    # 使用可切換的虛構輸入，分別驗證拒絕及復原流程。
    terminal_input = "fixture key"

    # 由測試替代隱藏的終端輸入，儲存與讀取仍使用實際檔案。
    def entered_key(_: str) -> str:
        return terminal_input

    monkeypatch.setattr(getpass, "getpass", entered_key)
    assert credentials.main(["set-key"]) == 1
    assert path.read_bytes() == broken_content
    terminal_input = "AQ.fixture-recovery"
    assert credentials.main(["set-key"]) == 0
    repaired = read_key(path)
    assert repaired is not None
    assert repaired.value == terminal_input
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# 跨程序鎖必須使用私有的實際目錄，拒絕目錄連結及權限過寬的路徑。
def test_insecure_key_directory(tmp_path: Path) -> None:
    # 建立權限過寬的目錄，以及指向私有父目錄的符號連結。
    target = tmp_path / "unsafe"
    target.mkdir(mode=0o755)
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)

    # 不安全的目錄或符號連結都應在建立 Key 前拒絕。
    for directory in (target, link):
        path = directory / "api-key"
        with pytest.raises((CredentialError, OSError)):
            credentials.set_key(path, "fixture-key")
        assert not path.exists()
