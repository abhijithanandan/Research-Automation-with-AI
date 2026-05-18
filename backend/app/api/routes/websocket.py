"""WebSocket event endpoint. See SPEC.md §4."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.workflow import subscribe_project, unsubscribe_project
from app.utils.logging import get_logger

_log = get_logger(__name__)

router = APIRouter(tags=["websocket"])


@router.websocket("/projects/{project_id}/events")
async def project_events(project_id: UUID, ws: WebSocket) -> None:
    """Stream workflow events to the client.

    Auth handshake: client sends `{"type":"auth","token":"<firebase-id-token>"}`.
    Server replies `{"type":"auth.ok"}` or closes with code 4401.

    After auth, server pushes events; client only sends `{"type":"ping"}`.
    """
    await ws.accept()

    # ---- Auth handshake (SPEC §4) ----------------------------------------
    try:
        first_msg = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
    except (TimeoutError, Exception):
        await ws.close(code=4401, reason="auth timeout")
        return

    if first_msg.get("type") != "auth" or not first_msg.get("token"):
        await ws.close(code=4401, reason="expected auth message")
        return

    from app.api.deps import _stable_uuid_from_uid
    from app.db.session import get_session
    from app.models.db import ProjectRow
    from app.services.auth import verify_firebase_token

    try:
        claims = await verify_firebase_token(str(first_msg["token"]))
    except Exception:
        await ws.close(code=4401, reason="invalid token")
        return

    uid = claims.get("uid")
    if not uid:
        await ws.close(code=4401, reason="invalid token")
        return

    user_id = _stable_uuid_from_uid(str(uid))

    async with get_session() as db:
        project = await db.get(ProjectRow, project_id)
        if project is None or project.owner_id != user_id:
            await ws.close(code=4403, reason="unauthorized for project")
            return

    await ws.send_json({"type": "auth.ok"})
    _log.info("ws_connected", project_id=str(project_id))

    # ---- Subscribe to project events -------------------------------------
    queue = subscribe_project(project_id)

    # Run two concurrent tasks: forwarding events and reading pings.
    async def _send_events() -> None:
        while True:
            event = await queue.get()
            try:
                await ws.send_json(event)
            except Exception:
                break

    async def _read_pings() -> None:
        while True:
            try:
                msg = await ws.receive_json()
                if msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
            except (WebSocketDisconnect, Exception):
                break

    sender = asyncio.create_task(_send_events())
    reader = asyncio.create_task(_read_pings())

    try:
        _done, pending = await asyncio.wait([sender, reader], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe_project(project_id)
        _log.info("ws_disconnected", project_id=str(project_id))
