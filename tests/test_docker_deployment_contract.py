import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_compose_supports_release_image_and_source_build() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    development = yaml.safe_load((ROOT / "compose.dev.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"fnos-media-import", "fnos-rclone-worker"}

    app = services["fnos-media-import"]
    assert app["image"] == "aichuguang/fnos-search:${APP_VERSION:-latest}"
    assert "build" not in app
    assert app["container_name"] == "fnos-media-import"
    assert app["environment"] == {
        "TZ": "${TZ:-Asia/Shanghai}",
        "APP_ENV": "production",
        "SECURITY_STRICT": "true",
        "APP_SECRET_KEY": "${APP_SECRET_KEY:-}",
        "NOTIFICATION_ENCRYPTION_KEY": "${NOTIFICATION_ENCRYPTION_KEY:-}",
        "RCLONE_WORKER_CONTROL_TOKEN": "${RCLONE_WORKER_CONTROL_TOKEN:-}",
        "FNOS_PROCESS_ROLE": "web",
        "RCLONE_WORKER_URL": "http://fnos-rclone-worker:5251",
        "LOG_FILE": "/app/logs/web.log",
    }

    development_app = development["services"]["fnos-media-import"]
    assert development_app["image"] == "${APP_IMAGE:-fnos-search-local}:${APP_VERSION:-dev}"
    assert development_app["build"]["context"] == "."

    app_volumes = {str(value) for value in app["volumes"]}
    assert "${APP_CONFIG_HOST_PATH:-./config}:/app/config" in app_volumes
    assert "${APP_DATA_HOST_PATH:-./data}:/app/data" in app_volumes
    assert "${APP_LOGS_HOST_PATH:-./logs}:/app/logs" in app_volumes
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
        "RCLONE_WORKER_CONTROL_TOKEN",
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
    assert '"/app/data/.secrets/notification_encryption_key"' in entrypoint_source
    assert '"/app/data/.secrets/rclone_worker_control_token"' in entrypoint_source
    assert 'db.set_app_settings({"app.secret_key": generated})' in app_source


def test_runtime_image_contains_rclone_without_docker_cli() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts" / "container_entrypoint.py").read_text(encoding="utf-8")
    worker_script = (ROOT / "scripts" / "fnos_rclone_worker.sh").read_text(encoding="utf-8")

    assert "ARG RCLONE_IMAGE=rclone/rclone:1.70.3" in dockerfile
    assert "COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone" in dockerfile
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
