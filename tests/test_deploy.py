"""Static checks on the deployment files. Docker and Render are not run here; see VERIFIED_RESULTS.md."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_dockerfile_binds_the_platform_port_and_runs_as_non_root() -> None:
    dockerfile = (REPO / "Dockerfile").read_text()
    lines = [line for line in dockerfile.splitlines() if line and not line.startswith("#")]
    assert "USER dispatch" in lines
    assert lines.index("USER dispatch") < lines.index(next(line for line in lines if line.startswith("CMD")))
    command = next(line for line in lines if line.startswith("CMD"))
    assert "${PORT:-8000}" in command and "--host 0.0.0.0" in command and "exec uvicorn" in command
    assert "EXPOSE 8000" in lines and re.search(r"^ENV .*\bPORT=8000\b", dockerfile, re.M)


def test_render_blueprint_declares_the_free_docker_web_service() -> None:
    blueprint = (REPO / "render.yaml").read_text()
    for setting in (
        "type: web",
        "name: dispatch-service",
        "runtime: docker",
        "plan: free",
        "healthCheckPath: /health",
        "dockerfilePath: ./Dockerfile",
    ):
        assert setting in blueprint, setting
    env = dict(re.findall(r"- key: (\S+)\n\s+value: (.*)", blueprint))
    assert env["DISPATCH_DATABASE_URL"].startswith("sqlite:////tmp/")
    assert env["DISPATCH_CORS_ORIGINS"] == '""' and env["DISPATCH_WEBHOOK_URL"] == '""'  # blank on purpose
    assert "no persistent disk" in blueprint
