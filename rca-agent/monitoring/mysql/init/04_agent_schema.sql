-- =============================================================================
-- RCA Agent - LLM agent run & thought persistence
-- =============================================================================
-- Mirrors Dify's MessageAgentThought design: every reasoning step and tool
-- observation is persisted, so a diagnosis can be replayed and audited.
-- =============================================================================

USE creditcard;

-- Agent run instance
CREATE TABLE IF NOT EXISTS rca_agent_run (
    run_id         VARCHAR(64)  PRIMARY KEY,
    incident_id    VARCHAR(64),
    alert_message  TEXT,
    severity       VARCHAR(8)   DEFAULT 'P1',
    mode           VARCHAR(16)  DEFAULT 'agentic',   -- deterministic | agentic
    status         VARCHAR(24)  DEFAULT 'running',   -- completed | timeout | budget_exceeded | failed | fallback
    route_reason   TEXT,
    steps_used     INT DEFAULT 0,
    tools_used     TEXT,
    total_tokens   INT DEFAULT 0,
    -- 输入/输出分开记：两者单价差 4~5 倍，只记 total 无法按模型精算费用。
    -- 老环境由此处的幂等迁移补齐（见 agent_store._ensure_columns）。
    prompt_tokens     INT DEFAULT 0,
    completion_tokens INT DEFAULT 0,
    latency_ms     INT DEFAULT 0,
    confidence     DECIMAL(4,3) DEFAULT 0,
    model          VARCHAR(64),
    seed_agreement TINYINT DEFAULT NULL,
    root_entity    VARCHAR(128),
    root_category  VARCHAR(32),
    final_answer   JSON,
    error          TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_run_incident  (incident_id),
    INDEX idx_run_created   (created_at),
    INDEX idx_run_mode      (mode),
    INDEX idx_run_status    (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Agent reasoning / action steps
CREATE TABLE IF NOT EXISTS rca_agent_thought (
    thought_id     BIGINT PRIMARY KEY AUTO_INCREMENT,
    run_id         VARCHAR(64) NOT NULL,
    step_no        INT,
    phase          VARCHAR(16),        -- observe | reason | act
    reasoning      TEXT,               -- LLM chain-of-thought (reasoning_content)
    tool_name      VARCHAR(64),
    tool_input     JSON,
    observation    TEXT,
    observation_ok TINYINT DEFAULT 1,
    tokens         INT DEFAULT 0,
    latency_ms     INT DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_thought_run (run_id, step_no),
    CONSTRAINT fk_thought_run FOREIGN KEY (run_id)
        REFERENCES rca_agent_run(run_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
