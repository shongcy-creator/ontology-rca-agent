-- =============================================================================
-- RCA Agent — 事件与反馈持久化
-- =============================================================================
-- 在 creditcard 库中创建 RCA 相关表。幂等（IF NOT EXISTS）。
-- =============================================================================

USE creditcard;

-- ── RCA 事件记录 ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rca_incident (
    incident_id     VARCHAR(64)  PRIMARY KEY,
    alert_message   TEXT,
    severity        VARCHAR(8)   DEFAULT 'P1',
    root_category   VARCHAR(32),
    root_entity     VARCHAR(128),
    confidence      DECIMAL(4,3) DEFAULT 0,
    elapsed_ms      DECIMAL(10,1) DEFAULT 0,
    topology_nodes  INT DEFAULT 0,
    topology_edges  INT DEFAULT 0,
    payload         JSON,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_rca_created  (created_at),
    INDEX idx_rca_category (root_category),
    INDEX idx_rca_severity (severity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 运维人员反馈（用于本体自演化）─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rca_feedback (
    feedback_id   BIGINT PRIMARY KEY AUTO_INCREMENT,
    incident_id   VARCHAR(64) NOT NULL,
    verdict       VARCHAR(16) NOT NULL,   -- CONFIRMED | REJECTED | PARTIAL
    actual_cause  VARCHAR(256),
    note          TEXT,
    operator      VARCHAR(64),
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_fb_incident (incident_id),
    INDEX idx_fb_verdict  (verdict),
    CONSTRAINT fk_fb_incident FOREIGN KEY (incident_id)
        REFERENCES rca_incident(incident_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 分析轨迹（自演化输入：30 条或 7 天触发一轮）───────────────────────────
CREATE TABLE IF NOT EXISTS rca_trajectory (
    trajectory_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    incident_id   VARCHAR(64) NOT NULL,
    step_no       INT,
    step_type     VARCHAR(32),
    description   TEXT,
    payload       JSON,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_traj_incident (incident_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
