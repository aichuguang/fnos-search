from __future__ import annotations


BASE_SCHEMA_SQL = r"""
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    keyword TEXT,
                    source TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    url TEXT NOT NULL,
                    password TEXT,
                    size INTEGER,
                    raw_data TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_resources_keyword ON resources(keyword);
                CREATE INDEX IF NOT EXISTS idx_resources_source_type ON resources(source_type);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_resources_unique_url_source ON resources(source, url);

                CREATE TABLE IF NOT EXISTS import_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    category_label TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    password TEXT,
                    target_route TEXT NOT NULL,
                    target_path TEXT,
                    status TEXT NOT NULL,
                    external_task_id TEXT,
                    error_message TEXT,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON import_jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_source_type ON import_jobs(source_type);
                CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON import_jobs(created_at);
                DROP INDEX IF EXISTS idx_jobs_unique_source_category;
                CREATE INDEX IF NOT EXISTS idx_jobs_source_category ON import_jobs(source_url, category);

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES import_jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_events_job_id ON job_events(job_id);
                CREATE INDEX IF NOT EXISTS idx_job_events_created_at_id ON job_events(created_at, id);

                CREATE TABLE IF NOT EXISTS rclone_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_reason TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    exit_code INTEGER,
                    error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_rclone_runs_started_at ON rclone_runs(started_at);

                CREATE TABLE IF NOT EXISTS rclone_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES rclone_runs(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_rclone_events_run_id ON rclone_events(run_id);
                CREATE INDEX IF NOT EXISTS idx_rclone_events_created_at_id ON rclone_events(created_at, id);

                CREATE TABLE IF NOT EXISTS rclone_file_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    job_id INTEGER,
                    status TEXT NOT NULL,
                    level TEXT NOT NULL,
                    category TEXT,
                    filename TEXT NOT NULL,
                    source_path TEXT,
                    target_path TEXT,
                    message TEXT,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES rclone_runs(id) ON DELETE SET NULL,
                    FOREIGN KEY(job_id) REFERENCES import_jobs(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_rclone_file_events_run_id ON rclone_file_events(run_id);
                CREATE INDEX IF NOT EXISTS idx_rclone_file_events_job_id ON rclone_file_events(job_id);
                CREATE INDEX IF NOT EXISTS idx_rclone_file_events_status ON rclone_file_events(status);
                CREATE INDEX IF NOT EXISTS idx_rclone_file_events_category ON rclone_file_events(category);
                CREATE INDEX IF NOT EXISTS idx_rclone_file_events_created_at_id ON rclone_file_events(created_at, id);

                CREATE TABLE IF NOT EXISTS search_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    keyword TEXT,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_url_hash TEXT NOT NULL,
                    password TEXT,
                    size TEXT,
                    raw_data TEXT,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_search_cache_public_id ON search_cache(public_id);
                CREATE INDEX IF NOT EXISTS idx_search_cache_expires_at ON search_cache(expires_at);
                CREATE INDEX IF NOT EXISTS idx_search_cache_url_hash ON search_cache(source_url_hash);

                CREATE TABLE IF NOT EXISTS guest_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_token TEXT NOT NULL UNIQUE,
                    job_id INTEGER,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    category_label TEXT,
                    source_type TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_url_hash TEXT NOT NULL,
                    password TEXT,
                    note TEXT,
                    status TEXT NOT NULL,
                    public_status TEXT NOT NULL,
                    client_ip_hash TEXT,
                    user_agent TEXT,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES import_jobs(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_guest_requests_token ON guest_requests(request_token);
                CREATE INDEX IF NOT EXISTS idx_guest_requests_status ON guest_requests(status);
                CREATE INDEX IF NOT EXISTS idx_guest_requests_job_id ON guest_requests(job_id);
                CREATE INDEX IF NOT EXISTS idx_guest_requests_url_hash ON guest_requests(source_url_hash);

                CREATE TABLE IF NOT EXISTS guest_request_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES guest_requests(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_guest_request_events_request_id ON guest_request_events(request_id);
                CREATE INDEX IF NOT EXISTS idx_guest_request_events_created_at_id ON guest_request_events(created_at, id);

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduler_leases (
                    name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_scheduler_leases_expires_at
                ON scheduler_leases(expires_at);

                CREATE TABLE IF NOT EXISTS organizer_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    request_id INTEGER,
                    rclone_run_id INTEGER,
                    trigger_type TEXT,
                    category TEXT NOT NULL,
                    category_label TEXT,
                    title TEXT,
                    source_keyword TEXT,
                    openlist_root_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL DEFAULT 0,
                    media_type TEXT,
                    tmdb_id INTEGER,
                    tmdb_title TEXT,
                    tmdb_year TEXT,
                    error_message TEXT,
                    evidence TEXT,
                    raw_data TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    scan_owner TEXT,
                    scan_lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES import_jobs(id) ON DELETE SET NULL,
                    FOREIGN KEY(request_id) REFERENCES guest_requests(id) ON DELETE SET NULL,
                    FOREIGN KEY(rclone_run_id) REFERENCES rclone_runs(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_organizer_tasks_status ON organizer_tasks(status);
                CREATE INDEX IF NOT EXISTS idx_organizer_tasks_job_id ON organizer_tasks(job_id);
                CREATE INDEX IF NOT EXISTS idx_organizer_tasks_root ON organizer_tasks(openlist_root_path);

                CREATE TABLE IF NOT EXISTS organizer_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    parent_path TEXT,
                    ext TEXT,
                    size INTEGER,
                    season INTEGER,
                    episode INTEGER,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES organizer_tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_organizer_files_task_id ON organizer_files(task_id);

                CREATE TABLE IF NOT EXISTS organizer_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    file_id INTEGER,
                    source_path TEXT NOT NULL,
                    source_name TEXT,
                    target_path TEXT NOT NULL,
                    target_name TEXT,
                    media_type TEXT,
                    title TEXT,
                    year TEXT,
                    season INTEGER,
                    episode INTEGER,
                    tmdb_id INTEGER,
                    confidence REAL DEFAULT 0,
                    status TEXT NOT NULL,
                    reason TEXT,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES organizer_tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(file_id) REFERENCES organizer_files(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_organizer_mappings_task_id ON organizer_mappings(task_id);
                CREATE INDEX IF NOT EXISTS idx_organizer_mappings_status ON organizer_mappings(status);

                CREATE TABLE IF NOT EXISTS organizer_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    mapping_id INTEGER,
                    run_id INTEGER,
                    type TEXT NOT NULL,
                    source_path TEXT,
                    target_path TEXT,
                    description TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    error_message TEXT,
                    undo_data TEXT,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES organizer_tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(mapping_id) REFERENCES organizer_mappings(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_organizer_operations_task_id ON organizer_operations(task_id);
                CREATE INDEX IF NOT EXISTS idx_organizer_operations_status ON organizer_operations(status);

                CREATE TABLE IF NOT EXISTS organizer_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    owner_id TEXT,
                    heartbeat_at TEXT,
                    lease_expires_at TEXT,
                    task_revision INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    summary TEXT,
                    undo_data TEXT,
                    error_message TEXT,
                    FOREIGN KEY(task_id) REFERENCES organizer_tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_organizer_runs_task_id ON organizer_runs(task_id);

                CREATE TABLE IF NOT EXISTS organizer_locks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lock_key TEXT NOT NULL UNIQUE,
                    task_id INTEGER,
                    run_id INTEGER,
                    owner TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS organizer_ai_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    provider TEXT,
                    model TEXT,
                    prompt TEXT,
                    response TEXT,
                    parsed TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES organizer_tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS organizer_tmdb_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    query TEXT,
                    media_type TEXT,
                    tmdb_id INTEGER,
                    title TEXT,
                    year TEXT,
                    score REAL,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES organizer_tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS update_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    category_label TEXT,
                    media_type TEXT NOT NULL,
                    season INTEGER,
                    year TEXT,
                    tmdb_id INTEGER,
                    query_template TEXT,
                    aliases TEXT,
                    schedule_kind TEXT NOT NULL,
                    days_of_week TEXT,
                    time_of_day TEXT,
                    interval_minutes INTEGER,
                    timezone TEXT NOT NULL,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    last_success_at TEXT,
                    next_episode INTEGER,
                    last_success_episode INTEGER,
                    missing_episodes TEXT,
                    source_strategy TEXT NOT NULL,
                    auto_import_policy TEXT NOT NULL,
                    min_score INTEGER DEFAULT 75,
                    quality_profile TEXT,
                    include_keywords TEXT,
                    exclude_keywords TEXT,
                    status TEXT NOT NULL,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS update_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    name TEXT,
                    url TEXT,
                    password TEXT,
                    provider TEXT,
                    priority INTEGER DEFAULT 100,
                    enabled INTEGER DEFAULT 1,
                    options TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(subscription_id) REFERENCES update_subscriptions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS update_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    trigger_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scheduled_at TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    candidate_count INTEGER DEFAULT 0,
                    imported_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    stage TEXT,
                    run_log TEXT,
                    summary TEXT,
                    raw_data TEXT,
                    owner_id TEXT,
                    heartbeat_at TEXT,
                    lease_expires_at TEXT,
                    FOREIGN KEY(subscription_id) REFERENCES update_subscriptions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS update_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    run_id INTEGER,
                    job_id INTEGER,
                    source_id INTEGER,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_url_hash TEXT NOT NULL,
                    password TEXT,
                    season INTEGER,
                    episode INTEGER,
                    size_text TEXT,
                    published_at TEXT,
                    score INTEGER DEFAULT 0,
                    decision TEXT NOT NULL,
                    reason TEXT,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(subscription_id) REFERENCES update_subscriptions(id) ON DELETE CASCADE,
                    FOREIGN KEY(run_id) REFERENCES update_runs(id) ON DELETE SET NULL,
                    FOREIGN KEY(job_id) REFERENCES import_jobs(id) ON DELETE SET NULL,
                    FOREIGN KEY(source_id) REFERENCES update_sources(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS update_seen_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    source_type TEXT,
                    source_url_hash TEXT,
                    file_id TEXT,
                    file_name TEXT,
                    size INTEGER,
                    season INTEGER,
                    episode INTEGER,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    raw_data TEXT,
                    UNIQUE(subscription_id, fingerprint),
                    FOREIGN KEY(subscription_id) REFERENCES update_subscriptions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS update_preview_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_url_hash TEXT NOT NULL,
                    source_url TEXT,
                    ok INTEGER DEFAULT 0,
                    message TEXT,
                    items_json TEXT,
                    latest_season INTEGER,
                    latest_episode INTEGER,
                    raw_data TEXT,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type, source_url_hash)
                );

                CREATE TABLE IF NOT EXISTS update_path_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    openlist_path TEXT NOT NULL,
                    files_json TEXT,
                    latest_season INTEGER,
                    latest_episode INTEGER,
                    raw_data TEXT,
                    captured_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    UNIQUE(subscription_id, openlist_path),
                    FOREIGN KEY(subscription_id) REFERENCES update_subscriptions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS update_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER,
                    run_id INTEGER,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(subscription_id) REFERENCES update_subscriptions(id) ON DELETE CASCADE,
                    FOREIGN KEY(run_id) REFERENCES update_runs(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_update_events_created_at_id ON update_events(created_at, id);

                CREATE TABLE IF NOT EXISTS rate_limit_buckets (
                    bucket_key TEXT PRIMARY KEY,
                    window_started_at INTEGER NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_rate_limit_buckets_expires_at
                ON rate_limit_buckets(expires_at);

"""
