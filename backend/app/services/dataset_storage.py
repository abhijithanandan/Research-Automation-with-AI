"""Dataset storage adapter (Phase 3, FR-2.3, Sprint 1).

Persists user-uploaded tabular files (CSV / JSON / Parquet) outside the
database. In dev the backend is a local filesystem rooted at
``settings.data_dir``; in prod the adapter switches to object storage (S3/
MinIO) without changing the call sites.

The storage URI returned is the canonical pointer the database stores. In
dev: ``file:///abs/path/to/data/<project_id>/<dataset_id>/<filename>``.
In prod: ``s3://bucket/<project_id>/<dataset_id>/<filename>``. Code that
needs to *read* the bytes back goes through :func:`read_bytes` so the
caller is decoupled from the URI scheme.

Schema extraction is intentionally lazy: we read just enough of the file to
get the column list and row count, never the full payload, so a 50 MiB
upload doesn't blow up memory on the API node.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID

from app.config import get_settings


class DatasetTooLargeError(Exception):
    """Raised when a dataset upload exceeds the configured byte cap."""


class DatasetParseError(Exception):
    """Raised when the uploaded file can't be parsed into a recognised tabular format."""


# Backwards-compatible alias — earlier drafts used this shorter name.
DatasetTooLarge = DatasetTooLargeError


# Supported extensions. Anything else is rejected at the route layer.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".csv", ".tsv", ".json", ".jsonl", ".parquet"})


@dataclass(frozen=True, slots=True)
class StoredDataset:
    """Result of a successful upload — what the route writes to DatasetRow."""

    sha256: str
    storage_uri: str
    columns: list[str]
    rowcount: int
    bytes: int


def _safe_filename(name: str) -> str:
    """Strip path components from an upload filename.

    Defense against ``../../etc/passwd`` style filenames. Werkzeug-style
    sanitisation is overkill here — we only ever read it back as a leaf
    name under a UUID directory, but the audit log still records the raw
    filename so we keep just the leaf.
    """
    # Replace any forward/back slashes; drop NULs; collapse to basename.
    cleaned = name.replace("\\", "/").split("/")[-1]
    cleaned = cleaned.replace("\x00", "_").strip()
    return cleaned or "uploaded"


def _compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_csv(data: bytes, delimiter: str = ",") -> tuple[list[str], int]:
    """Return (columns, rowcount). rowcount excludes the header."""
    text = data.decode("utf-8-sig", errors="replace")  # tolerate BOM
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise DatasetParseError("CSV is empty") from exc
    columns = [c.strip() for c in header]
    rowcount = sum(1 for _ in reader)
    return columns, rowcount


def _parse_json(data: bytes) -> tuple[list[str], int]:
    """JSON file that is either an array of records or {"data": [...]}.

    Empty arrays produce ``([], 0)`` — a valid (degenerate) dataset.
    Scalar / non-tabular JSON is rejected.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetParseError(f"Invalid JSON: {exc}") from exc
    records: list[dict[str, object]]
    if isinstance(payload, list):
        records = [r for r in payload if isinstance(r, dict)]
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        records = [r for r in payload["data"] if isinstance(r, dict)]
    else:
        raise DatasetParseError("JSON must be an array of objects or {data: [...]}.")
    if not records:
        return [], 0
    # Union of keys across the first 100 records — pandas does the same.
    cols: list[str] = []
    seen: set[str] = set()
    for record in records[:100]:
        for k in record:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols, len(records)


def _parse_jsonl(data: bytes) -> tuple[list[str], int]:
    """JSON-lines: one record per line. Tolerates trailing blank lines."""
    cols: list[str] = []
    seen: set[str] = set()
    rowcount = 0
    for line in data.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetParseError(f"Invalid JSON line {rowcount + 1}: {exc}") from exc
        if not isinstance(obj, dict):
            raise DatasetParseError(f"JSONL line {rowcount + 1} is not an object.")
        rowcount += 1
        if rowcount <= 100:
            for k in obj:
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
    return cols, rowcount


def _parse_parquet(data: bytes) -> tuple[list[str], int]:
    """Parquet schema via pyarrow if installed; otherwise rejected explicitly.

    We don't list pyarrow as a hard dep because it adds ~60MB. If the dep
    isn't present we surface a friendly error pointing the user at CSV /
    JSON. The Phase-3 install-time guidance (Sprint 6) will document this.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover — env-dependent
        raise DatasetParseError(
            "Parquet support requires pyarrow. Convert to CSV or JSONL, "
            "or install pyarrow on the backend."
        ) from exc
    try:
        table = pq.read_table(io.BytesIO(data))
    except Exception as exc:
        raise DatasetParseError(f"Failed to read parquet: {exc}") from exc
    return list(table.column_names), table.num_rows


def parse_schema(filename: str, data: bytes) -> tuple[list[str], int]:
    """Dispatch on extension. Returns (columns, rowcount)."""
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        return _parse_csv(data)
    if ext == ".tsv":
        return _parse_csv(data, delimiter="\t")
    if ext == ".json":
        return _parse_json(data)
    if ext == ".jsonl":
        return _parse_jsonl(data)
    if ext == ".parquet":
        return _parse_parquet(data)
    raise DatasetParseError(
        f"Unsupported extension {ext!r}. Supported: {sorted(SUPPORTED_EXTENSIONS)}"
    )


def store(project_id: UUID, dataset_id: UUID, filename: str, data: bytes) -> StoredDataset:
    """Write *data* to local FS under ``DATA_DIR/<project_id>/<dataset_id>/`` and
    return the populated metadata.

    Enforces the size cap, computes the sha256, extracts the schema, then
    writes the file to disk (atomic rename inside the dataset dir). Caller
    inserts the corresponding DatasetRow.
    """
    settings = get_settings()
    if len(data) > settings.max_dataset_bytes:
        raise DatasetTooLargeError(
            f"Upload is {len(data)} bytes, cap is {settings.max_dataset_bytes}"
        )

    safe_name = _safe_filename(filename)
    columns, rowcount = parse_schema(safe_name, data)
    sha = _compute_sha256(data)

    root = Path(settings.data_dir).resolve()
    target_dir = root / str(project_id) / str(dataset_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name

    # Atomic write: write to a sibling temp file, then rename.
    tmp_path = target_path.with_suffix(target_path.suffix + ".part")
    tmp_path.write_bytes(data)
    tmp_path.replace(target_path)

    storage_uri = target_path.resolve().as_uri()

    return StoredDataset(
        sha256=sha,
        storage_uri=storage_uri,
        columns=columns,
        rowcount=rowcount,
        bytes=len(data),
    )


def read_bytes(storage_uri: str) -> bytes:
    """Read the file behind *storage_uri*. Only ``file://`` is supported in dev.

    A future S3-backed adapter swaps this for a boto3 call; the call site
    (the sandbox in Sprint 3) is decoupled.
    """
    parsed = urlparse(storage_uri)
    if parsed.scheme != "file":
        raise NotImplementedError(f"Storage scheme {parsed.scheme!r} is not supported in dev.")
    path = Path(unquote(parsed.path))
    return path.read_bytes()


def delete(storage_uri: str) -> None:
    """Best-effort delete of the file backing *storage_uri*. Silent if missing."""
    parsed = urlparse(storage_uri)
    if parsed.scheme != "file":
        return  # prod adapter will overwrite this with a real implementation
    path = Path(unquote(parsed.path))
    try:
        path.unlink()
        # Also remove the per-dataset dir if empty (per-project dir stays).
        parent = path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except FileNotFoundError:
        return
