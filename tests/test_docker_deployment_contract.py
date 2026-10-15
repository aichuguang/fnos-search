import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_documented_transfer_timeouts_reach_both_application_containers() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    timeouts = dict(re.findall(
        r"^(TRANSFER_[A-Z_]+_SECONDS)=(\d+)$",
        (ROOT / ".env.example").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    ))
    assert len(timeouts) == 4
    for service_name in ("fnos-media-import", "fnos-rclone-worker"):
        environment = compose["services"][service_name]["environment"]
        for name, default in timeouts.items():
            assert environment.get(name) == "${" + name + ":-" + default + "}", (service_name, name)


def test_web_configuration_repair_does_not_wait_for_business_readiness() -> None:
    from fnos_media_import.services.readiness_service import ReadinessService

    worker_status = ReadinessService(
        process_role="all",
        database_probe=lambda: {},
        docker_socket_mounted=lambda: False,
        root_key_probe=lambda: True,
        openlist_required=lambda: True,
        valid_openlist_mounts_probe=lambda: 0,
        local_worker_status=lambda: {"success": True, "healthy": True},
    ).status()
    assert worker_status["ok"] is False
    assert worker_status["checks"]["openlist"]["status"] == "unconfigured"

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    dependency = compose["services"]["fnos-media-import"]["depends_on"]["fnos-rclone-worker"]
    assert dependency["condition"] == "service_healthy"
    worker_healthcheck = compose["services"]["fnos-rclone-worker"]["healthcheck"]
    # The HTTP server only starts after shared DB initialization. Do not race it
    # with service_started, or gate the repair UI on business configuration.
    assert "127.0.0.1:5251/livez" in " ".join(worker_healthcheck["test"])
    assert "/readyz" not in " ".join(worker_healthcheck["test"])
    # Keep the readiness endpoint and web health check strict for monitoring.
    assert "127.0.0.1:5251/readyz" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_compose_supports_release_image_and_source_build() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    development = yaml.safe_load((ROOT / "compose.dev.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"fnos-media-import", "fnos-rclone-worker", "pansou"}

    pansou = services["pansou"]
    assert pansou["profiles"] == ["pansou"]
    assert "ports" not in pansou
    assert pansou["environment"]["AUTH_ENABLED"] == "true"

    app = services["fnos-media-import"]
    assert app["image"] == "aichuguang/fnos-search:${APP_VERSION:-latest}"
    assert "build" not in app
    assert app["container_name"] == "fnos-media-import"
    assert app["environment"] == {
        "TZ": "${TZ:-Asia/Shanghai}",
        "APP_ENV": "production",
        "SECURITY_STRICT": "true",
        "RCLONE_WORKER_CONTROL_TOKEN": "${RCLONE_WORKER_CONTROL_TOKEN:-}",
        "FNOS_ROOT_KEY": "${FNOS_ROOT_KEY:-}",
        "FNOS_RELAY_ROOT": "/data/relay",
        "FNOS_LOCAL_TARGET_ROOT": "/storage-targets",
        "TRANSFER_SOURCE_VISIBLE_TIMEOUT_SECONDS": "${TRANSFER_SOURCE_VISIBLE_TIMEOUT_SECONDS:-1800}",
        "TRANSFER_NATIVE_WAIT_TIMEOUT_SECONDS": "${TRANSFER_NATIVE_WAIT_TIMEOUT_SECONDS:-604800}",
        "TRANSFER_WAIT_TIMEOUT_SECONDS": "${TRANSFER_WAIT_TIMEOUT_SECONDS:-10800}",
        "TRANSFER_CANCEL_TIMEOUT_SECONDS": "${TRANSFER_CANCEL_TIMEOUT_SECONDS:-3600}",
        "FNOS_PROCESS_ROLE": "web",
        "RCLONE_WORKER_URL": "http://fnos-rclone-worker:5251",
        "LOG_FILE": "/app/logs/web.log",
        "PANSOU_BASE_URL": "http://pansou:8888",
        "PANSOU_USERNAME": "${PANSOU_USERNAME:-fnos-search}",
        "PANSOU_PASSWORD": "${PANSOU_PASSWORD:-fnos-search}",
    }

    development_app = development["services"]["fnos-media-import"]
    assert development_app["image"] == "${APP_IMAGE:-fnos-search-local}:${APP_VERSION:-dev}"
    assert development_app["build"]["context"] == "."

    app_volumes = {str(value) for value in app["volumes"]}
    assert "${APP_CONFIG_HOST_PATH:-./config}:/app/config" in app_volumes
    assert "${APP_DATA_HOST_PATH:-./data}:/app/data" in app_volumes
    assert "${APP_LOGS_HOST_PATH:-./logs}:/app/logs" in app_volumes
    assert "${FNOS_RELAY_HOST_PATH:-./rclone/relay}:/data/relay" in app_volumes
    assert "${RCLONE_TEMP_HOST_PATH:-./rclone/temp}:/temp" not in app_volumes
    assert not any("docker.sock" in value for value in app_volumes)

    rclone = services["fnos-rclone-worker"]
    assert rclone["container_name"] == "fnos-rclone-worker"
    assert rclone["image"] == "aichuguang/fnos-search:${APP_VERSION:-latest}"
    assert "ports" not in rclone
    assert rclone["environment"]["FNOS_PROCESS_ROLE"] == "all"
    assert rclone["environment"]["RCLONE_CONFIG_PATH"] == "/config/rclone/rclone.conf"
    assert rclone["environment"]["LOG_FILE"] == "/app/logs/worker.log"
    assert {
        "${RCLONE_CONFIG_HOST_PATH:-./rclone/config}:/config/rclone",
        "${RCLONE_TEMP_HOST_PATH:-./rclone/temp}:/temp",
        "${RCLONE_CACHE_HOST_PATH:-./rclone/cache}:/cache",
        "${FNOS_RELAY_HOST_PATH:-./rclone/relay}:/data/relay",
    } <= {str(value) for value in rclone["volumes"]}
    assert all(
        "docker.sock" not in str(volume)
        for service in services.values()
        for volume in service.get("volumes", [])
    )


def test_release_workflow_validates_before_multi_arch_publish() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    assert {"validate", "publish"} <= set(jobs)
    assert jobs["publish"]["needs"] == "validate"

    publish_step = next(
        step
        for step in jobs["publish"]["steps"]
        if step.get("uses") == "docker/build-push-action@v6"
    )
    assert publish_step["with"]["push"] is True
    assert publish_step["with"]["platforms"] == "linux/amd64,linux/arm64"


def test_optional_env_example_only_exposes_common_user_settings() -> None:
    assignments = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert assignments == {
        "APP_VERSION",
        "APP_PORT",
        "TZ",
        "APP_CONFIG_HOST_PATH",
        "APP_DATA_HOST_PATH",
        "APP_LOGS_HOST_PATH",
        "RCLONE_CONFIG_HOST_PATH",
        "RCLONE_CACHE_HOST_PATH",
        "RCLONE_TEMP_HOST_PATH",
        "FNOS_ROOT_KEY",
        "FNOS_RELAY_HOST_PATH",
        "RCLONE_LOCAL_STORAGE_HOST_PATH",
        "TRANSFER_SOURCE_VISIBLE_TIMEOUT_SECONDS",
        "TRANSFER_NATIVE_WAIT_TIMEOUT_SECONDS",
        "TRANSFER_WAIT_TIMEOUT_SECONDS",
        "TRANSFER_CANCEL_TIMEOUT_SECONDS",
        "PANSOU_USERNAME",
        "PANSOU_PASSWORD",
    }


def test_readme_documents_every_compose_variable() -> None:
    variable_pattern = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-[^}]*)?\}")
    compose_sources = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("docker-compose.yml", "compose.dev.yaml")
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    variables = set(variable_pattern.findall(compose_sources))

    undocumented = sorted(name for name in variables if f"`{name}`" not in readme)
    assert undocumented == []


def test_data_mount_persists_database_and_generated_secrets() -> None:
    config_source = (ROOT / "fnos_media_import" / "config.py").read_text(encoding="utf-8")
    entrypoint_source = (ROOT / "scripts" / "container_entrypoint.py").read_text(encoding="utf-8")
    app_source = (ROOT / "fnos_media_import" / "app.py").read_text(encoding="utf-8")

    assert '"database_path": "data/app.db"' in config_source
    assert '"/app/data/.secrets/rclone_worker_control_token"' in entrypoint_source
    assert "_validate_root_key()" in entrypoint_source
    assert '"/data/relay"' in entrypoint_source
    assert "10001:10001" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "NOTIFICATION_ENCRYPTION_KEY" not in entrypoint_source
    assert "app_session_key = derive_key(root_key, PURPOSE_APP_SESSION).hex()" in app_source
    assert "app.secret_key = app_session_key" in app_source
    assert 'db.set_app_settings({"app.secret_key": generated})' not in app_source


def test_runtime_image_contains_rclone_without_docker_cli() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts" / "container_entrypoint.py").read_text(encoding="utf-8")
    worker_script = (ROOT / "scripts" / "fnos_rclone_worker.sh").read_text(encoding="utf-8")

    assert "ARG RCLONE_IMAGE=rclone/rclone:1.70.3" in dockerfile
    assert "COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone" in dockerfile
    assert "RUN command -v flock >/dev/null" in dockerfile
    assert "DOCKER_CLI_IMAGE" not in dockerfile
    assert "/var/run/docker.sock" not in compose
    assert "/var/run/docker.sock" not in entrypoint
    assert "RCLONE_EXECUTION_MODE" not in worker_script
    assert "docker exec" not in worker_script
    assert 'rclone "$@" --config "$RCLONE_CONFIG_PATH" --cache-dir "$RCLONE_CACHE_DIR"' in worker_script
    assert "127.0.0.1:5251/readyz" in dockerfile


def test_upgrade_and_sqlite_constraints_are_explicit() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "必须同时替换 `docker-compose.yml`" in readme
    assert "只有在新编排已经生效后" in readme
    assert "SMB、NFS、CIFS" in readme
    assert "web.log" in readme
    assert "worker.log" in readme


def test_docker_build_context_excludes_runtime_data_and_secrets() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {".git", ".env*", "*.db", "*.db-*", "data", "logs", "rclone/config/rclone.conf"} <= patterns
