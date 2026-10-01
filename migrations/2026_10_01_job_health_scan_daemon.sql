-- Insert scan_daemon row into job_health so job_heartbeat.py can probe it.
-- stall_minutes=5 matches daemon STALL_THRESHOLD_S=300; alert threshold = 3x = 15min.
-- ON CONFLICT is a no-op if the row already exists (idempotent).

INSERT INTO job_health (
    job_key,
    display_name,
    category,
    systemd_unit,
    enabled,
    status,
    stall_minutes,
    detail,
    updated_at
) VALUES (
    'scan_daemon',
    'Scan Daemon (kjle-scan-daemon)',
    'daemon',
    'kjle-scan-daemon',
    TRUE,
    'unknown',
    5,
    'awaiting first heartbeat probe',
    NOW()
)
ON CONFLICT (job_key) DO NOTHING;
