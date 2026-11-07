-- 啟用紀錄只保留目前設定的結果與驗證請求的開始順序。
CREATE TABLE IF NOT EXISTS activation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    key_version uuid,
    fingerprint text,
    provider text,
    model text,
    status text NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified', 'ready', 'invalid')),
    probe_sequence bigint NOT NULL DEFAULT 0,
    verified_sequence bigint NOT NULL DEFAULT 0
);

-- 每日全服務用量與每筆請求的送達狀態分開記錄。
CREATE TABLE IF NOT EXISTS daily_model_quota (
    service_day date PRIMARY KEY,
    used integer NOT NULL CHECK (used BETWEEN 0 AND 100)
);

CREATE TABLE IF NOT EXISTS model_requests (
    id uuid PRIMARY KEY,
    service_day date NOT NULL REFERENCES daily_model_quota (service_day),
    delivery text NOT NULL DEFAULT 'unknown' CHECK (delivery IN ('unknown', 'sent', 'unsent'))
);

INSERT INTO activation DEFAULT VALUES ON CONFLICT DO NOTHING;

-- 初始角色與帳號識別固定；資料庫只保存帶鹽的密碼雜湊。
CREATE TABLE IF NOT EXISTS demo_accounts (
    username text PRIMARY KEY CHECK (username IN ('user', 'support')),
    password_hash text NOT NULL
);

-- Cookie 只以摘要索引；閒置與絕對期限都以 UTC 時間點比較。
CREATE TABLE IF NOT EXISTS login_sessions (
    token_digest bytea PRIMARY KEY,
    username text NOT NULL REFERENCES demo_accounts (username),
    created_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL
);

-- 來源與帳號各自累計；帳號桶保存正規化名稱的摘要，避免保留原始輸入。
CREATE TABLE IF NOT EXISTS login_rate_limits (
    dimension text NOT NULL CHECK (dimension IN ('source', 'account')),
    bucket text NOT NULL,
    attempts integer NOT NULL CHECK (attempts > 0),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (dimension, bucket)
);
