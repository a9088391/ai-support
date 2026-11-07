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
