CREATE DATABASE IF NOT EXISTS creditcard;
USE creditcard;

CREATE TABLE IF NOT EXISTS t_customer (
    customer_id   BIGINT PRIMARY KEY AUTO_INCREMENT,
    name          VARCHAR(128) NOT NULL,
    card_no       VARCHAR(32)  NOT NULL UNIQUE,
    credit_limit  DECIMAL(12,2) NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS t_txn (
    txn_id        BIGINT PRIMARY KEY AUTO_INCREMENT,
    customer_id   BIGINT NOT NULL,
    amount        DECIMAL(12,2) NOT NULL,
    status        VARCHAR(16) NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_txn_customer (customer_id),
    INDEX idx_txn_created (created_at)
);

CREATE TABLE IF NOT EXISTS t_audit (
    audit_id      BIGINT PRIMARY KEY AUTO_INCREMENT,
    txn_id        BIGINT NOT NULL,
    event_type    VARCHAR(32) NOT NULL,
    payload       JSON,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_audit_txn (txn_id)
);

INSERT INTO t_customer (name, card_no, credit_limit) VALUES
    ('Alice',  '4111111111111111', 50000.00),
    ('Bob',    '4222222222222222', 30000.00),
    ('Carol',  '4333333333333333', 15000.00);

INSERT INTO t_txn (customer_id, amount, status) VALUES
    (1, 1200.50, 'SETTLED'),
    (2, 880.00,  'PENDING'),
    (3, 450.00,  'SETTLED');

INSERT INTO t_audit (txn_id, event_type, payload) VALUES
    (1, 'AUTHORIZED', '{"channel":"web"}'),
    (1, 'CAPTURED',   '{"ref":"CH-001"}'),
    (2, 'AUTHORIZED', '{"channel":"mobile"}');
