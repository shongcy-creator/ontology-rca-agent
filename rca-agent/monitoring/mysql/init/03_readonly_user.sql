-- =============================================================================
-- RCA Agent - read-only forensics account
-- =============================================================================
-- Least privilege: this account is for diagnosis only, with NO write ability.
--
--   PROCESS            -> SHOW ENGINE INNODB STATUS, information_schema.innodb_trx
--   SELECT perf_schema -> slow query digest (events_statements_summary_by_digest)
--   SELECT creditcard  -> table structure / indexes / stats
--   REPLICATION CLIENT -> replication status (optional)
--
-- Account name/password must match backend env RCA_DB_USER / RCA_DB_PASSWORD.
-- NOTE: information_schema read access is implicit; explicit GRANT is rejected
--       with ERROR 1044, so it is intentionally omitted.
-- =============================================================================

CREATE USER IF NOT EXISTS 'rca_readonly'@'%' IDENTIFIED BY 'rca_readonly_pwd';

GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'rca_readonly'@'%';
GRANT SELECT ON creditcard.* TO 'rca_readonly'@'%';
GRANT SELECT ON performance_schema.* TO 'rca_readonly'@'%';

FLUSH PRIVILEGES;

-- Also grant forensics privileges to the business account so the agent can
-- still degrade gracefully when RCA_DB_USER is not configured.
GRANT PROCESS ON *.* TO 'appuser'@'%';
GRANT SELECT ON performance_schema.* TO 'appuser'@'%';

FLUSH PRIVILEGES;
