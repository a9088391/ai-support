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
    used integer NOT NULL CONSTRAINT daily_model_quota_used_nonnegative CHECK (used >= 0)
);

CREATE TABLE IF NOT EXISTS model_requests (
    id uuid PRIMARY KEY,
    service_day date NOT NULL REFERENCES daily_model_quota (service_day),
    delivery text NOT NULL DEFAULT 'unknown' CHECK (delivery IN ('unknown', 'sent', 'unsent'))
);

INSERT INTO activation DEFAULT VALUES ON CONFLICT DO NOTHING;

-- 僅舊版升級鎖住用量表；先等共用交易鎖，避免與先結算明細的取消操作互等。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'daily_model_quota'::regclass AND conname = 'daily_model_quota_used_check'
    ) THEN
        PERFORM pg_advisory_xact_lock(7680281636712970524);
        ALTER TABLE daily_model_quota
            DROP CONSTRAINT daily_model_quota_used_check,
            ADD CONSTRAINT daily_model_quota_used_nonnegative CHECK (used >= 0);
    END IF;
END;
$$;

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

-- 每列保存文件目前版本與其片段；同批匯入只替換本表。
CREATE TABLE IF NOT EXISTS knowledge_documents (
    id text PRIMARY KEY,
    title text NOT NULL,
    version text NOT NULL,
    fragments jsonb NOT NULL
);

-- 啟用探測的帳號欄為空，沿用原有歷史用量與送達紀錄。
ALTER TABLE model_requests
    ADD COLUMN IF NOT EXISTS username text REFERENCES demo_accounts (username);

-- 已確認拒絕只在原請求保留 Key 版本與驗證序號；既有紀錄的這兩欄維持空值。
ALTER TABLE model_requests
    ADD COLUMN IF NOT EXISTS rejected_key_version uuid,
    ADD COLUMN IF NOT EXISTS rejected_verified_sequence bigint;

CREATE INDEX IF NOT EXISTS model_requests_rejected_binding
    ON model_requests (rejected_key_version, rejected_verified_sequence)
    WHERE rejected_key_version IS NOT NULL;

-- 到期後連同對話所屬問題與嘗試一併移除。
CREATE TABLE IF NOT EXISTS conversations (
    id uuid PRIMARY KEY,
    username text NOT NULL REFERENCES demo_accounts (username),
    created_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    question text NOT NULL
);

CREATE INDEX IF NOT EXISTS questions_by_conversation ON questions (conversation_id);

-- 暫存快照獨立於可更新的知識文件；配額不隨暫存刪除。
CREATE TABLE IF NOT EXISTS answer_attempts (
    id uuid PRIMARY KEY,
    question_id uuid NOT NULL REFERENCES questions (id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('running', 'stopped', 'answered',
        'insufficient_evidence', 'invalid_output', 'rate_limited', 'timed_out', 'unavailable')),
    answer text,
    citations jsonb NOT NULL DEFAULT '[]',
    search_results jsonb NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS answer_attempts_by_question ON answer_attempts (question_id);

-- 舊版每段對話只有一題；位置可沿用 1，且舊回答沒有可安全送回的模型步驟。
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS key_version uuid;
ALTER TABLE questions ADD COLUMN IF NOT EXISTS position integer NOT NULL DEFAULT 1;
ALTER TABLE answer_attempts
    ADD COLUMN IF NOT EXISTS model_steps jsonb,
    ADD COLUMN IF NOT EXISTS attempt_number integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS deadline_at timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS questions_by_position ON questions (conversation_id, position);
CREATE UNIQUE INDEX IF NOT EXISTS attempts_by_number ON answer_attempts (question_id, attempt_number);

-- 工單快照永久保留，不參照會清理的問答或可更新的知識來源。
CREATE TABLE IF NOT EXISTS tickets (
    id uuid PRIMARY KEY,
    question_id uuid NOT NULL UNIQUE,
    attempt_id uuid NOT NULL,
    description text NOT NULL,
    question text NOT NULL,
    answer_status text NOT NULL,
    answer text,
    citations jsonb NOT NULL,
    search_results jsonb NOT NULL,
    created_by text NOT NULL REFERENCES demo_accounts (username),
    created_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS tickets_by_owner ON tickets (created_by, created_at DESC, id DESC);

-- 舊工單仍為 open，重跑初始化時保留已提交的結案結果與時間。
ALTER TABLE tickets
    ADD COLUMN IF NOT EXISTS resolution text,
    ADD COLUMN IF NOT EXISTS closed_at timestamptz;

-- 已核對的重送只保留識別，與工單同壽命，且不參照暫存問答。
CREATE TABLE IF NOT EXISTS ticket_attempt_replays (
    ticket_id uuid NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    attempt_id uuid NOT NULL,
    PRIMARY KEY (ticket_id, attempt_id)
);
