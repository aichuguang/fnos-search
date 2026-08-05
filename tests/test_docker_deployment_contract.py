from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_compose_supports_release_image_and_source_build() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    development = yaml.safe_load((ROOT / "compose.dev.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"fnos-media-import", "rclone-server"}

    app = services["fnos-media-import"]
    assert app["image"] == "${APP_IMAGE:-aichuguang/fnos-search}:${APP_VERSION:-latest}"
    assert "build" not in app
    assert app["environment"]["RCLONE_CONTAINER_NAME"] == "${RCLONE_CONTAINER_NAME:-rclone-server}"

    development_app = development["services"]["fnos-media-import"]
    assert development_app["image"] == "${APP_IMAGE:-fnos-search-local}:${APP_VERSION:-dev}"
    assert development_app["build"]["context"] == "."

    app_volumes = {str(value) for value in app["volumes"]}
    assert any(value.endswith(":/app/config") for value in app_volumes)
    assert any(value.endswith(":/app/data") for value in app_volumes)
    assert any(value.endswith(":/app/logs") for value in app_volumes)
    assert any(value.endswith(":/temp") for value in app_volumes)
    assert any(value.endswith(":/var/run/docker.sock") for value in app_volumes)

    rclone = services["rclone-server"]
    assert rclone["container_name"] == "${RCLONE_CONTAINER_NAME:-rclone-server}"
    assert rclone["image"] == "${RCLONE_IMAGE:-rclone/rclone:1.70.3}"


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


def test_docker_build_context_excludes_runtime_data_and_secrets() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {".git", ".env*", "*.db", "*.db-*", "data", "logs", "rclone/config/rclone.conf"} <= patterns
