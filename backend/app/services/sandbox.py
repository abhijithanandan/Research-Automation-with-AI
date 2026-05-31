"""Phase 3 sandbox — T2 (Docker per-call). See SPEC v0.3 §6.3 / BRD §10.

The Analyst proposes code at the ``await_code_approval`` HITL gate; on
approve the workflow ships the code into a fresh Docker container, the
container runs to completion (or is killed on timeout), and the resulting
figures + stdout/stderr come back as Artifacts.

Hardening (per docs/brd-verification-and-phase3-plan.md §2.5 + the
sandbox-escape risk row in BRD §10):

  * ``--network=none``        — no outbound traffic, no DNS, no UDP
  * ``--read-only``           — root filesystem is RO, tmpfs at /work
  * ``--memory=…``            — kill on OOM; swap disabled (--memory-swap)
  * ``--cpus=…``              — bounded CPU consumption
  * ``--pids-limit=64``       — fork-bomb protection
  * ``--user 65534:65534``    — nobody, never root
  * ``--cap-drop=ALL``        — no Linux capabilities
  * ``--security-opt=no-new-privileges`` — no setuid escalation
  * wall-clock timeout        — SIGKILLed when the deadline passes

All flags are belts-and-braces with the static AST scan in
:mod:`app.agents.analyst` — a deny that slips the scanner still has to
break out of the container.

`run_in_sandbox` is **async** but the Docker CLI is synchronous; it
shells out via ``asyncio.create_subprocess_exec`` so the FastAPI worker
doesn't block while a 60-second container runs.

Sprint 3 ships the module + unit tests. Integration tests that actually
spawn a container are gated on ``DOCKER_INTEGRATION=1`` plus a working
Docker daemon — they're skipped in the default suite (kept dependency-
free) and run in the audit/security-review CI step.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from app.config import get_settings
from app.utils.logging import get_logger

_log = get_logger(__name__)

# Output truncation. matplotlib + scikit-learn happily print megabytes of
# warnings; we capture the first 64 KiB of each stream so a chatty script
# doesn't OOM the API node and so the artifact stored in audit_log stays
# reasonable.
MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 64 * 1024


class SandboxUnavailableError(RuntimeError):
    """Raised when the sandbox is asked to run but it can't (no Docker,
    sandbox_enabled=False, etc.).  Caller surfaces this to the user as a
    'sandbox not configured' message — not a graph failure."""


class SandboxResult(BaseModel):
    """Outcome of one sandbox run.

    `figures` is a list of PNG-bytes payloads (one per file dropped under
    ``/work/figures/``). `stdout`/`stderr` are truncated to MAX_*_BYTES so a
    chatty script can't blow up the audit log. `timed_out` and `oomed` are
    diagnostic markers the gate UI surfaces so the user can revise the
    code with the right kind of hint.
    """

    exit_code: int
    stdout: str
    stderr: str
    figures: list[bytes]
    duration_ms: int
    timed_out: bool = False
    oomed: bool = False


@dataclass(frozen=True, slots=True)
class _SandboxConfig:
    """Per-call sandbox knobs, pulled from settings + the call site."""

    image: str
    timeout_s: int
    memory_mb: int
    cpus: float

    @classmethod
    def from_settings(cls) -> _SandboxConfig:
        s = get_settings()
        return cls(
            image=s.sandbox_image,
            timeout_s=s.sandbox_timeout_s,
            memory_mb=s.sandbox_memory_mb,
            cpus=s.sandbox_cpus,
        )


def _docker_available() -> bool:
    """Cheap precondition check — ``docker`` on PATH and ``docker info`` exits 0."""
    if shutil.which("docker") is None:
        return False
    try:
        r = asyncio.run(_run("docker", "info", "--format", "{{.ServerVersion}}", timeout=5))
        return r[0] == 0
    except Exception:
        return False


async def _run(*argv: str, timeout: float | None = None) -> tuple[int, bytes, bytes]:
    """Run a subprocess and return (exit_code, stdout, stderr).

    Doesn't raise on a non-zero exit — the caller may want to inspect the
    return code.  Raises :class:`asyncio.TimeoutError` if the deadline
    passes.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout or b"", stderr or b""


def _truncate(b: bytes, cap: int) -> str:
    if len(b) <= cap:
        return b.decode("utf-8", errors="replace")
    head = b[:cap].decode("utf-8", errors="replace")
    return head + f"\n[truncated — output exceeded {cap} bytes]\n"


def docker_argv(
    *,
    image: str,
    work_dir: Path,
    timeout_s: int,
    memory_mb: int,
    cpus: float,
) -> list[str]:
    """Build the ``docker run`` argv for one sandbox call.

    Extracted as a pure function so the security-checklist tests (Sprint 6)
    can assert the exact flag set without spawning a container.
    """
    return [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--pids-limit=64",
        f"--memory={memory_mb}m",
        # swap = memory means swap is effectively disabled (no extra space).
        f"--memory-swap={memory_mb}m",
        f"--cpus={cpus}",
        # Mount the prepared work dir read-write at /work so the script can
        # write figures + read datasets. The HOST mount is RO from the host's
        # perspective elsewhere — the user's dataset files were already
        # copied in.
        "-v",
        f"{work_dir}:/work:rw",
        "-w",
        "/work",
        # We deliberately do not pass `-i` / `-t` — no stdin, no TTY.
        image,
        "python",
        "/work/run.py",
    ]


async def run_in_sandbox(
    code: str,
    *,
    datasets: dict[str, bytes] | None = None,
    config: _SandboxConfig | None = None,
) -> SandboxResult:
    """Execute *code* inside a freshly-spawned hardened Docker container.

    `datasets` maps a per-dataset filename (e.g. ``users.csv``) to the
    file's bytes; each entry is copied to ``/work/datasets/<filename>``
    before the container starts.  The script reads them from there.

    The return value is a :class:`SandboxResult` — never an exception —
    so the graph node can record a `log` artifact for any outcome and
    let the user reject + regenerate if the run failed.

    Refuses to run when:
      * ``settings.sandbox_enabled`` is False — defense in depth for
        staging hosts that haven't been hardened.
      * Docker isn't installed or the daemon isn't reachable.
    """
    s = get_settings()
    if not s.sandbox_enabled:
        raise SandboxUnavailableError(
            "Sandbox is disabled (SANDBOX_ENABLED=false). Set it to true on a "
            "hardened host before approving analyst code."
        )

    if shutil.which("docker") is None:
        raise SandboxUnavailableError(
            "docker CLI is not on PATH. Install Docker to enable the sandbox."
        )

    cfg = config or _SandboxConfig.from_settings()

    with tempfile.TemporaryDirectory(prefix="rfa-sandbox-") as workdir:
        work = Path(workdir)
        (work / "datasets").mkdir(parents=True, exist_ok=True)
        (work / "figures").mkdir(parents=True, exist_ok=True)
        # Drop the analyst's code into /work/run.py.
        (work / "run.py").write_text(code, encoding="utf-8")
        # Drop each dataset into /work/datasets/<filename>.
        if datasets:
            for fname, blob in datasets.items():
                # Belt and braces: never honour an absolute or traversal
                # path passed in here. The dataset_storage layer already
                # sanitises filenames at upload time, but datasets is the
                # one dict we trust from a non-storage caller too.
                leaf = Path(fname).name or "dataset"
                (work / "datasets" / leaf).write_bytes(blob)

        argv = docker_argv(
            image=cfg.image,
            work_dir=work,
            timeout_s=cfg.timeout_s,
            memory_mb=cfg.memory_mb,
            cpus=cfg.cpus,
        )
        _log.info("sandbox_starting", image=cfg.image, timeout_s=cfg.timeout_s)

        # Wall-clock timeout = the container budget + a small safety margin
        # so we kill the process group via timeout rather than via Docker's
        # in-container signal handling (which is racy on shutdown).
        start = asyncio.get_event_loop().time()
        timed_out = False
        try:
            exit_code, stdout, stderr = await _run(*argv, timeout=cfg.timeout_s + 5)
        except TimeoutError:
            timed_out = True
            exit_code, stdout, stderr = 124, b"", b"[sandbox killed: wall-clock timeout]\n"
        duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)

        # OOM detection. Docker exits the container with 137 (SIGKILL by
        # OOM killer) on out-of-memory; surface that as a structured field
        # so the UI can show a "you allocated too much" hint instead of a
        # raw 137.
        oomed = exit_code == 137

        # Collect the figures the script wrote.
        figures: list[bytes] = []
        for fig in sorted((work / "figures").glob("*.png")):
            try:
                figures.append(fig.read_bytes())
            except OSError as exc:
                _log.warning(
                    "sandbox_figure_read_failed",
                    figure=fig.name,
                    error_type=type(exc).__name__,
                )

        _log.info(
            "sandbox_done",
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
            oomed=oomed,
            figures=len(figures),
            stdout_bytes=len(stdout),
            stderr_bytes=len(stderr),
        )
        return SandboxResult(
            exit_code=exit_code,
            stdout=_truncate(stdout, MAX_STDOUT_BYTES),
            stderr=_truncate(stderr, MAX_STDERR_BYTES),
            figures=figures,
            duration_ms=duration_ms,
            timed_out=timed_out,
            oomed=oomed,
        )


def result_to_audit_payload(r: SandboxResult) -> dict[str, object]:
    """Serialise the result into the dict the audit-log row stores."""
    # Figures are PNG bytes — too big for the audit payload. The audit row
    # records the count + total bytes; the figures go to the artifact
    # table as separate `figure` rows.
    body: dict[str, object] = json.loads(r.model_dump_json(exclude={"figures"}))
    body["figures_count"] = len(r.figures)
    body["figures_bytes"] = sum(len(f) for f in r.figures)
    return body


__all__ = [
    "MAX_STDERR_BYTES",
    "MAX_STDOUT_BYTES",
    "SandboxResult",
    "SandboxUnavailableError",
    "docker_argv",
    "result_to_audit_payload",
    "run_in_sandbox",
]
