"""Preflight dependency check — run before pytest in CI.

A third-party reviewer once had a misconfigured venv where pytest crashed at
*collection* time on missing optional deps (pydantic_settings, respx, etc.).
That kind of failure is easy to miss because the error surfaces as a
``ModuleNotFoundError`` somewhere deep in the import chain, and the test
runner exits non-zero with no per-test detail.

This script enumerates every module the test suite needs and surfaces a
clean, single-line error per missing module. Wire it into ``run_ci_local.sh``
before ``pytest`` so the bootstrap problem fails loud and fast.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

REQUIRED_MODULES: tuple[str, ...] = (
    # Runtime
    "fastapi",
    "pydantic",
    "pydantic_settings",
    "sqlalchemy",
    "alembic",
    "httpx",
    "structlog",
    "langgraph",
    "langgraph.checkpoint",
    "langchain",
    "chromadb",
    "thefuzz",
    "tenacity",
    "email_validator",
    "google.genai",
    "anthropic",
    "pypdf",
    "firebase_admin",
    "asyncpg",
    "psycopg",
    # Phase 2 hybrid search — sparse BM25 retrieval (core dep). The
    # cross-encoder reranker (sentence_transformers) is an OPTIONAL extra and
    # is intentionally NOT listed here: default CI runs without it and the
    # retrieval path degrades to RRF-only, so requiring it would wrongly fail
    # preflight on a lean install.
    "rank_bm25",
    # Dev / test
    "pytest",
    "pytest_asyncio",
    "pytest_cov",
    "respx",
    "aiosqlite",
    "jsonschema",
    "mypy",
    "ruff",
)


# Hard minimum Python version. SPEC.md §2.1 + BRD §7 — Python 3.11 baseline
# (asyncio.TaskGroup, StrEnum, TypeAlias semantics). Newer is fine; older is
# not (the type-hint syntax we use throughout breaks on 3.10).
_MIN_PY: tuple[int, int] = (3, 11)

# Compatibility-sensitive trio. A reviewer hit a *PydanticDeprecationWarning
# import from pydantic* failure — i.e. langchain-core / langgraph pulling a
# symbol that a mismatched pydantic version no longer exports. That is NOT a
# "missing module" error; it's a version-drift error and surfaces as an
# ImportError deep in the import chain. We import these three together and
# report version drift distinctly so it is never confused with "run pip
# install". Keep the version floors aligned with pyproject's pins.
_COMPAT_TRIO: tuple[tuple[str, str], ...] = (
    ("pydantic", "2.11"),
    ("langchain_core", "1.0"),
    ("langgraph", "1.0"),
)


def _check_python_version() -> str | None:
    actual = sys.version_info[:2]
    if actual < _MIN_PY:
        return f"Python {_MIN_PY[0]}.{_MIN_PY[1]}+ required, got {actual[0]}.{actual[1]}"
    return None


def _check_interpreter() -> str | None:
    """Warn (not fail) if we are clearly not in the project virtualenv.

    The single most common cause of "27 collection errors" is running pytest
    with the *system* interpreter where the deps were never installed, while a
    perfectly good project ``.venv`` sits one directory up. We can't force the
    interpreter from here, but we can name the problem precisely so the next
    line ("MISSING: …") isn't misread as "the project is broken".
    """
    venv = os.environ.get("VIRTUAL_ENV")
    backend_root = Path(__file__).resolve().parents[1]
    expected_venv = backend_root / ".venv"
    in_venv = sys.prefix != sys.base_prefix  # venv/virtualenv set this
    if not in_venv and expected_venv.exists():
        return (
            f"Not running inside a virtualenv (sys.prefix == base_prefix), but a "
            f"project venv exists at {expected_venv}. You are probably using the "
            f"system interpreter ({sys.executable}). Activate the venv first."
        )
    if venv and expected_venv.exists() and Path(venv).resolve() != expected_venv.resolve():
        return (
            f"Active venv ({venv}) is not the project venv ({expected_venv}). "
            f"Dependency versions may not match the pins."
        )
    return None


def _check_compat_trio() -> list[str]:
    """Import the version-drift-prone trio and report mismatches distinctly."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    def _tuple(v: str) -> tuple[int, ...]:
        out: list[int] = []
        for part in v.split(".")[:2]:
            digits = "".join(c for c in part if c.isdigit())
            out.append(int(digits) if digits else 0)
        return tuple(out)

    problems: list[str] = []
    for dist, floor in _COMPAT_TRIO:
        try:
            installed = pkg_version(dist)
        except PackageNotFoundError:
            problems.append(f"{dist}: not installed (need >= {floor})")
            continue
        if _tuple(installed) < _tuple(floor):
            problems.append(
                f"{dist} {installed} is below the supported floor {floor} — "
                f"version drift; bump it to match pyproject pins"
            )
    return problems


def main() -> int:
    # Interpreter check first — it explains every downstream error if wrong.
    interp_warning = _check_interpreter()
    if interp_warning is not None:
        sys.stderr.write("\n========= PREFLIGHT: INTERPRETER WARNING =========\n")
        sys.stderr.write(f"  {interp_warning}\n")
        sys.stderr.write(
            "\nFix: cd backend && source .venv/Scripts/activate "
            "(Windows) / source .venv/bin/activate (POSIX), then re-run.\n"
        )
        # Don't return yet — fall through so the MISSING list also prints,
        # giving the operator the complete picture in one shot.

    py_error = _check_python_version()
    if py_error is not None:
        sys.stderr.write("\n========= PREFLIGHT FAILED =========\n")
        sys.stderr.write(f"  {py_error}\n")
        sys.stderr.write("\nUpgrade Python before continuing.\n")
        return 1

    missing: list[tuple[str, str]] = []
    for mod in REQUIRED_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError as exc:
            missing.append((mod, str(exc)))

    compat_problems = _check_compat_trio()

    if missing or compat_problems:
        sys.stderr.write("\n========= PREFLIGHT FAILED =========\n")
        for mod, exc in missing:
            sys.stderr.write(f"  MISSING: {mod}  ({exc})\n")
        for problem in compat_problems:
            sys.stderr.write(f"  VERSION DRIFT: {problem}\n")
        sys.stderr.write(
            "\nRun: pip install -e '.[dev]' from the backend/ directory "
            "(inside the project .venv).\n"
            "If you are inside Docker, rebuild: docker compose build backend.\n"
        )
        if interp_warning is not None:
            sys.stderr.write(
                "NOTE: the interpreter warning above is almost certainly the "
                "root cause — fix the venv first, then the MISSING list clears.\n"
            )
        return 1
    print(
        f"preflight ok — python {sys.version_info[0]}.{sys.version_info[1]}, "
        f"{len(REQUIRED_MODULES)} required modules importable, "
        f"compat trio (pydantic/langchain-core/langgraph) version-aligned"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
