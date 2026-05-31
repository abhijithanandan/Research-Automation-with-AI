"""Sprint-3 tests for the Phase-3 sandbox.

Two layers:

  * **Pure-function tests** — the `docker_argv` builder and the result
    serializer. No subprocess, no Docker — these run in the default
    pytest suite and gate the hardening flag set.

  * **Integration tests** — actually spawn a container, asserted against
    the security checklist (no-network, OOM kill, timeout kill, FS RO,
    output truncation). Gated on ``DOCKER_INTEGRATION=1`` plus a
    reachable Docker daemon and the ``researchflow-analyst:0.2`` image.
    They're skipped in the default suite (keeps CI dependency-free) and
    run as the security-review CI step (Sprint 6).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.config import get_settings
from app.services.sandbox import (
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    SandboxResult,
    SandboxUnavailableError,
    docker_argv,
    result_to_audit_payload,
    run_in_sandbox,
)

# ---------------------------------------------------------------------------
# Pure-function tests — no Docker required
# ---------------------------------------------------------------------------


def test_docker_argv_includes_all_hardening_flags(tmp_path: Path) -> None:
    """Every flag the security checklist (Sprint 6) requires must appear."""
    argv = docker_argv(
        image="researchflow-analyst:0.2",
        work_dir=tmp_path,
        timeout_s=60,
        memory_mb=512,
        cpus=1.0,
    )
    joined = " ".join(argv)

    assert "--rm" in argv
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--user=65534:65534" in argv
    assert "--pids-limit=64" in argv
    assert "--memory=512m" in argv
    assert "--memory-swap=512m" in argv  # swap disabled
    assert "--cpus=1.0" in argv
    assert f"{tmp_path}:/work:rw" in joined
    # The script is the only entry — no shell wrapping, no -i, no -t.
    assert argv[-3:] == ["researchflow-analyst:0.2", "python", "/work/run.py"]


def test_docker_argv_respects_caller_overrides(tmp_path: Path) -> None:
    """Image / cap / memory / cpu come from the caller, not hardcoded."""
    argv = docker_argv(
        image="custom:1.2",
        work_dir=tmp_path,
        timeout_s=30,
        memory_mb=1024,
        cpus=2.5,
    )
    assert "custom:1.2" in argv
    assert "--memory=1024m" in argv
    assert "--memory-swap=1024m" in argv
    assert "--cpus=2.5" in argv


def test_run_refuses_when_sandbox_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """SANDBOX_ENABLED=false (the default) must reject every call."""
    s = get_settings()
    monkeypatch.setattr(s, "sandbox_enabled", False)
    import asyncio

    with pytest.raises(SandboxUnavailableError, match="disabled"):
        asyncio.run(run_in_sandbox("print('hi')\n"))


def test_run_refuses_when_docker_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `docker` isn't on PATH the call must refuse with a clear error."""
    s = get_settings()
    monkeypatch.setattr(s, "sandbox_enabled", True)
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kw: None)
    import asyncio

    with pytest.raises(SandboxUnavailableError, match="docker CLI"):
        asyncio.run(run_in_sandbox("print('hi')\n"))


def test_result_audit_payload_strips_figure_bytes() -> None:
    """Figures are too large for the audit row — payload must record
    count + total bytes, never the PNG payload."""
    r = SandboxResult(
        exit_code=0,
        stdout="hello",
        stderr="",
        figures=[b"\x89PNG..." + b"x" * 1000, b"\x89PNG..." + b"y" * 500],
        duration_ms=42,
    )
    payload = result_to_audit_payload(r)
    assert "figures" not in payload
    assert payload["figures_count"] == 2
    assert payload["figures_bytes"] == len(r.figures[0]) + len(r.figures[1])
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "hello"


def test_output_truncation_constants() -> None:
    """Output cap is 64 KiB per stream — anything bigger gets a truncation
    marker. Bound is exposed so the Sprint-5 UI can reuse it."""
    assert MAX_STDOUT_BYTES == 64 * 1024
    assert MAX_STDERR_BYTES == 64 * 1024


# ---------------------------------------------------------------------------
# Integration tests — only run when Docker is present
# ---------------------------------------------------------------------------

_DOCKER_AVAILABLE = (
    shutil.which("docker") is not None and os.environ.get("DOCKER_INTEGRATION") == "1"
)
needs_docker = pytest.mark.skipif(
    not _DOCKER_AVAILABLE,
    reason="Set DOCKER_INTEGRATION=1 and ensure docker is on PATH to run sandbox integration tests",
)


@needs_docker
@pytest.mark.asyncio
async def test_sandbox_runs_simple_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke: a hello-world script runs to completion."""
    s = get_settings()
    monkeypatch.setattr(s, "sandbox_enabled", True)
    result = await run_in_sandbox("print('hello sandbox')\n")
    assert result.exit_code == 0
    assert "hello sandbox" in result.stdout
    assert result.figures == []
    assert not result.timed_out
    assert not result.oomed


@needs_docker
@pytest.mark.asyncio
async def test_sandbox_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Security checklist §2.6 item 1: --network=none blocks all outbound traffic."""
    s = get_settings()
    monkeypatch.setattr(s, "sandbox_enabled", True)
    code = (
        "import socket\n"
        "try:\n"
        "    socket.gethostbyname('example.com')\n"
        "    print('LEAK')\n"
        "except Exception as e:\n"
        "    print('blocked:', type(e).__name__)\n"
    )
    result = await run_in_sandbox(code)
    assert "LEAK" not in result.stdout
    assert "blocked" in result.stdout


@needs_docker
@pytest.mark.asyncio
async def test_sandbox_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Security checklist §2.6 item 3: an infinite loop is killed at the deadline."""
    from app.services.sandbox import _SandboxConfig

    s = get_settings()
    monkeypatch.setattr(s, "sandbox_enabled", True)
    cfg = _SandboxConfig(image=s.sandbox_image, timeout_s=2, memory_mb=128, cpus=0.5)
    result = await run_in_sandbox("while True:\n    pass\n", config=cfg)
    assert result.timed_out
    assert result.exit_code != 0
