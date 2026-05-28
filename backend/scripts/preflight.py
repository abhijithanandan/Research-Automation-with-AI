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
import sys

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


def _check_python_version() -> str | None:
    actual = sys.version_info[:2]
    if actual < _MIN_PY:
        return f"Python {_MIN_PY[0]}.{_MIN_PY[1]}+ required, got {actual[0]}.{actual[1]}"
    return None


def main() -> int:
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
    if missing:
        sys.stderr.write("\n========= PREFLIGHT FAILED =========\n")
        for mod, exc in missing:
            sys.stderr.write(f"  MISSING: {mod}  ({exc})\n")
        sys.stderr.write(
            "\nRun: pip install -e '.[dev]' from the backend/ directory.\n"
            "If you are inside Docker, rebuild: docker compose build backend.\n"
        )
        return 1
    print(
        f"preflight ok — python {sys.version_info[0]}.{sys.version_info[1]}, "
        f"{len(REQUIRED_MODULES)} required modules importable"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
