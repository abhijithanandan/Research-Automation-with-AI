"""WebSocket events endpoint. See SPEC.md §4."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.utils.logging import get_logger

router = APIRouter(tags=["websocket"])
log = get_logger(__name__)


@router.websocket("/projects/{project_id}/events")
async def project_events(ws: WebSocket, project_id: UUID) -> None:
    await ws.accept()

    # Auth handshake per SPEC.md §4. First message must be {"type":"auth", ...}.
    try:
        first = await ws.receive_json()
    except WebSocketDisconnect:
        return

    if first.get("type") != "auth" or not first.get("token"):
        await ws.close(code=4401)
        return

    # TODO: verify Firebase token and authorize project access.
    await ws.send_json({"type": "auth.ok", "ts": datetime.now(tz=UTC).isoformat()})

    log.info("ws.connected", project_id=str(project_id))

    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong", "ts": datetime.now(tz=UTC).isoformat()})
            # Server is the primary publisher; client messages other than ping are ignored.
    except WebSocketDisconnect:
        log.info("ws.disconnected", project_id=str(project_id))
