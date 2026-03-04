-- ============================================================
-- Claude API Usage Dashboard — Supabase Schema
-- ============================================================
-- Run this in: Supabase Dashboard → SQL Editor → New Query
-- ============================================================


-- Usage records (synced daily from Anthropic API)
CREATE TABLE IF NOT EXISTS usage_records (
    id                  UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    date                DATE        NOT NULL,
    model               VARCHAR(100) NOT NULL,
    workspace_id        VARCHAR(100) NOT NULL DEFAULT 'default',
    workspace_name      VARCHAR(255),
    input_tokens        BIGINT      DEFAULT 0,
    output_tokens       BIGINT      DEFAULT 0,
    total_tokens        BIGINT      DEFAULT 0,
    cost_usd            DECIMAL(12, 6) DEFAULT 0,
    request_count       INTEGER     DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT unique_usage_record UNIQUE (date, model, workspace_id)
);

CREATE INDEX IF NOT EXISTS idx_usage_date        ON usage_records (date DESC);
CREATE INDEX IF NOT EXISTS idx_usage_model       ON usage_records (model);
CREATE INDEX IF NOT EXISTS idx_usage_workspace   ON usage_records (workspace_id);


-- Sync log (one row per sync run — helps you debug issues)
CREATE TABLE IF NOT EXISTS sync_log (
    id              UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    sync_date       DATE        NOT NULL,
    status          VARCHAR(50) NOT NULL,   -- 'success', 'partial', 'error'
    records_synced  INTEGER     DEFAULT 0,
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ──────────────────────────────────────────────
-- Row Level Security
-- ──────────────────────────────────────────────
-- Allow public READ (dashboard is public) but only service role can WRITE.
-- If you want the dashboard to be private, change SELECT policies to use auth.

ALTER TABLE usage_records  ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_log       ENABLE ROW LEVEL SECURITY;

-- Public read
CREATE POLICY "public_read_usage"   ON usage_records  FOR SELECT USING (true);
CREATE POLICY "public_read_sync"    ON sync_log        FOR SELECT USING (true);

-- Service role write (used by your sync endpoint)
CREATE POLICY "service_write_usage" ON usage_records
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_write_sync" ON sync_log
    FOR ALL USING (auth.role() = 'service_role');


-- ──────────────────────────────────────────────
-- Helpful Views
-- ──────────────────────────────────────────────

-- Monthly cost summary by workspace
CREATE OR REPLACE VIEW monthly_cost_by_workspace AS
SELECT
    DATE_TRUNC('month', date) AS month,
    workspace_name,
    SUM(input_tokens)   AS input_tokens,
    SUM(output_tokens)  AS output_tokens,
    SUM(total_tokens)   AS total_tokens,
    ROUND(SUM(cost_usd)::numeric, 2) AS cost_usd,
    SUM(request_count)  AS request_count
FROM usage_records
GROUP BY 1, 2
ORDER BY 1 DESC, cost_usd DESC;


-- Monthly cost summary by model
CREATE OR REPLACE VIEW monthly_cost_by_model AS
SELECT
    DATE_TRUNC('month', date) AS month,
    model,
    SUM(input_tokens)   AS input_tokens,
    SUM(output_tokens)  AS output_tokens,
    SUM(total_tokens)   AS total_tokens,
    ROUND(SUM(cost_usd)::numeric, 2) AS cost_usd,
    SUM(request_count)  AS request_count
FROM usage_records
GROUP BY 1, 2
ORDER BY 1 DESC, cost_usd DESC;
