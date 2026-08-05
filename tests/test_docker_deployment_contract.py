import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_compose_supports_release_image_and_source_build() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    development = yaml.safe_load((ROOT / "compose.dev.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"fnos-media-import", "rclone-server"}

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
    }

    development_app = development["services"]["fnos-media-import"]
    assert development_app["image"] == "${APP_IMAGE:-fnos-search-local}:${APP_VERSION:-dev}"
    assert development_app["build"]["context"] == "."

    app_volumes = {str(value) for value in app["volumes"]}
    assert "${APP_CONFIG_HOST_PATH:-./config}:/app/config" in app_volumes
    assert "${APP_DATA_HOST_PATH:-./data}:/app/data" in app_volumes
    assert "${APP_LOGS_HOST_PATH:-./logs}:/app/logs" in app_volumes
    assert "${RCLONE_TEMP_HOST_PATH:-./rclone/temp}:/temp" in app_volumes
    assert any(value.endswith(":/var/run/docker.sock") for value in app_volumes)

    rclone = services["rclone-server"]
    assert rclone["container_name"] == "rclone-server"
    assert rclone["image"] == "rclone/rclone:1.70.3"
    assert {
        "${RCLONE_CONFIG_HOST_PATH:-./rclone/config}:/config/rclone",
        "${RCLONE_TEMP_HOST_PATH:-./rclone/temp}:/temp",
        "${RCLONE_CACHE_HOST_PATH:-./rclone/cache}:/cache",
    } <= {str(value) for value in rclone["volumes"]}


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
    assert 'db.set_app_settings({"app.secret_key": generated})' in app_source


def test_docker_build_context_excludes_runtime_data_and_secrets() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {".git", ".env*", "*.db", "*.db-*", "data", "logs", "rclone/config/rclone.conf"} <= patterns
