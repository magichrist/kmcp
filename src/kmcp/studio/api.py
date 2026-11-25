"""Studio REST API routes.

Mounts all Studio (Playbook Studio) REST endpoints on the main Starlette app
via ``mount_studio_routes(app)``. Called from ``src/kmcp/server.py``
lifespan startup.
"""

from __future__ import annotations

import hashlib
import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from kmcp.config import config
from kmcp.db import manager as db_manager
from kmcp.studio import (
    audit as audit_service,
)
from kmcp.studio import (
    redaction as redaction_service,
)
from kmcp.studio import (
    service_agents,
    service_connections,
    service_mcp_connections,
    service_tools,
)

logger = logging.getLogger("kmcp.studio.api")


# ══════════════════════════════════════════════════════════════════════════════
# Shared response / audit / input helpers
# ══════════════════════════════════════════════════════════════════════════════


def _json_response(payload: object, status_code: int = 200) -> JSONResponse:
    """JSON response with cross-cutting secret redaction on every egress."""
    return JSONResponse(redaction_service.redact(payload), status_code=status_code)


def _request_actor(request: Request) -> str | None:
    """Derive a non-secret actor label from the bearer token.

    The token itself is never logged; a short digest is enough to identify the
    caller in the audit trail. Returns None when no bearer token was supplied.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return "bearer:" + hashlib.sha256(token.encode()).hexdigest()[:12]
    return None


def _audit(
    db,
    event: str,
    actor: str | None,
    *,
    entity_type: str | None = None,
    entity_id: object = None,
    details: dict | None = None,
) -> None:
    """Append a redacted audit record without breaking the primary mutation."""
    try:
        audit_service.record_audit(
            db,
            event=event,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            actor=actor or "anonymous",
            details=redaction_service.redact(details or {}),
        )
    except Exception:
        logger.exception("audit record failed for event=%s", event)


def _parse_int(value: str | None, name: str, default: int, minimum: int = 0) -> int:
    """Parse a query/path integer, raising ValueError for invalid input."""
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}: expected an integer") from exc
    if parsed < minimum:
        raise ValueError(f"invalid {name}: must be >= {minimum}")
    return parsed


def _safe_auth_config(raw: object) -> dict | None:
    """Return a value-free summary of auth config (never echo secret values)."""
    if not raw:
        return None
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {"configured": True}
    if isinstance(parsed, dict):
        return {"configured": True, "fields": sorted(str(k) for k in parsed)}
    return {"configured": True}


def _sanitize_connection(row: dict) -> dict:
    """Strip secret values from a connection row before returning it."""
    out = dict(row)
    out["auth_config"] = _safe_auth_config(out.get("auth_config"))
    return out


#: Runtime debug flag toggled from the Studio settings page. ``config`` is a
#: frozen dataclass, so the live value is kept here and reflected by
#: ``GET /api/server`` so the UI always shows the truth.
_debug_runtime: dict[str, bool] = {"debug": bool(getattr(config, "debug", False))}


def _server_health() -> dict | None:
    """Best-effort live server health (uptime, active executions, …)."""
    try:
        from kmcp.server import health  # deferred: avoids import cycles
    except Exception:
        return None
    try:
        return health.get_status()
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Playbook CRUD routes
# ══════════════════════════════════════════════════════════════════════════════


async def list_playbooks(request: Request) -> JSONResponse:
    """List playbooks (with optional status/builtin filtering)."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    status = request.query_params.get("status")
    is_builtin = request.query_params.get("is_builtin")
    try:
        limit = _parse_int(request.query_params.get("limit"), "limit", 50, minimum=1)
        offset = _parse_int(request.query_params.get("offset"), "offset", 0)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)

    rows = db_manager.DBManager.list_playbooks(db, status=status)

    if is_builtin is not None:
        want = is_builtin.lower() in ("1", "true", "yes")
        rows = [r for r in rows if bool(r.get("is_builtin", 0)) == want]

    rows = rows[offset : offset + limit]
    return _json_response({"playbooks": rows, "limit": limit, "offset": offset})


async def get_playbook(request: Request) -> JSONResponse:
    """Get playbook metadata."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    playbook_id = request.path_params["id"]
    row = db.get_playbook(playbook_id)
    if row is None:
        return _json_response({"error": "playbook not found"}, status_code=404)
    return _json_response(row)


async def put_definition(request: Request) -> JSONResponse:
    """Save playbook definition (draft)."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    playbook_id = request.path_params["id"]
    body = await request.json()
    definition = body.get("definition")
    if not isinstance(definition, dict):
        return _json_response({"error": "definition is required"}, status_code=422)

    from kmcp.studio import playbook_service

    validation = playbook_service.validate_definition(db, playbook_id, definition)
    if not validation["valid"]:
        return _json_response(
            {"error": "definition failed validation", "details": validation["findings"]},
            status_code=422,
        )

    playbook_service.update_playbook(
        db, playbook_id, status="draft", draft_definition=json.dumps(definition)
    )
    _audit(
        db,
        "playbook.draft_saved",
        _request_actor(request),
        entity_type="playbook",
        entity_id=playbook_id,
    )

    return _json_response({"playbook_id": playbook_id, "valid": True})


async def validate_definition_api(request: Request) -> JSONResponse:
    """Validate a definition without saving it or changing playbook status."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    playbook_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid JSON"}, status_code=400)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        return _json_response({"error": "definition is required"}, status_code=422)

    from kmcp.studio import playbook_service

    validation = playbook_service.validate_definition(db, playbook_id, definition)
    return _json_response(validation)


async def get_definition_api(request: Request) -> JSONResponse:
    """Get playbook definition (draft if present, else latest published version)."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    playbook_id = request.path_params["id"]
    row = db.get_playbook(playbook_id)
    if row is None:
        return _json_response({"error": "playbook not found"}, status_code=404)

    # Prefer draft_definition if present
    draft = row.get("draft_definition")
    if draft:
        try:
            definition = json.loads(draft) if isinstance(draft, str) else draft
            return _json_response({"definition": definition, "draft": True})
        except (TypeError, ValueError):
            pass

    # Fall back to latest published version
    versions = db.list_playbook_versions(playbook_id)
    if versions:
        version = versions[-1]  # ordered oldest-first; last is latest
        definition = version.get("definition")
        if isinstance(definition, str):
            try:
                definition = json.loads(definition)
            except (TypeError, ValueError):
                definition = None
        return _json_response({"definition": definition, "draft": False})

    return _json_response({"error": "no definition found"}, status_code=404)


async def put_definition_publish(request: Request) -> JSONResponse:
    """Publish playbook definition."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    playbook_id = request.path_params["id"]
    body = await request.json()
    definition = body.get("definition")
    version = body.get("version", "1.0.0")

    if not isinstance(definition, dict):
        return _json_response({"error": "definition is required"}, status_code=422)

    from kmcp.studio import approvals, playbook_service

    # Validate before consuming the approval so a rejected definition does not
    # burn an operator's decision.
    validation = playbook_service.validate_definition(db, playbook_id, definition, for_publish=True)
    if not validation["valid"]:
        return _json_response(
            {"error": "definition failed validation", "details": validation["findings"]},
            status_code=422,
        )

    if not approvals.is_approved(db, approvals.GATE_PUBLISH, playbook_id):
        return _json_response(
            {
                "error": approvals.require_approval_for(approvals.GATE_PUBLISH)
                + " Create an approval request and wait for an operator to approve it."
            },
            status_code=403,
        )
    # Atomic claim: a single approval authorizes exactly one publish.
    if not approvals.consume_approval(db, approvals.GATE_PUBLISH, playbook_id):
        return _json_response(
            {"error": "approval was already consumed; request a new approval"},
            status_code=403,
        )

    vid = playbook_service.publish_version(
        db, playbook_id, version, definition, published_by="studio"
    )
    actor = _request_actor(request)
    _audit(
        db,
        "playbook.published",
        actor,
        entity_type="playbook",
        entity_id=playbook_id,
        details={"version": version, "version_id": vid},
    )

    return _json_response(
        {
            "playbook_id": playbook_id,
            "version_id": vid,
            "status": "published",
        }
    )


async def approval_request_api(request: Request) -> JSONResponse:
    """Request operator approval; publishing stays blocked until it is granted."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    from kmcp.studio import approvals

    actor = _request_actor(request) or "studio"
    playbook_id = request.path_params["id"]
    aid = approvals.request_approval(
        db,
        gate=approvals.GATE_PUBLISH,
        resource_id=playbook_id,
        requested_by=actor,
    )
    _audit(
        db,
        "approval.requested",
        actor,
        entity_type="approval",
        entity_id=aid,
        details={"gate": approvals.GATE_PUBLISH, "resource_id": playbook_id},
    )
    return _json_response(
        {
            "approval_id": aid,
            "playbook_id": playbook_id,
            "gate": approvals.GATE_PUBLISH,
            "status": "pending",
        }
    )


async def decide_approval_api(request: Request) -> JSONResponse:
    """Approve or reject a pending approval (operator-only decision endpoint).

    Authorization is enforced by ``TokenAuthMiddleware`` on the main app; the
    extra check here keeps the endpoint fail-closed if Studio is served from a
    listener without that middleware. The actor stored in the audit trail is a
    digest of the bearer token, never the token itself.
    """
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    actor = _request_actor(request)
    if (
        actor is None
        and getattr(config, "api_token", "")
        and not getattr(config, "insecure", False)
    ):
        return _json_response({"error": "Unauthorized"}, status_code=401)
    actor = actor or "anonymous"

    try:
        approval_id = _parse_int(request.path_params.get("id"), "approval_id", 0, minimum=1)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)

    decision = "rejected" if request.url.path.rstrip("/").endswith("/reject") else "approved"
    cur = db.conn.cursor()
    row = cur.execute(
        "SELECT gate, resource_id, status FROM pending_approvals WHERE id=?", (approval_id,)
    ).fetchone()
    if row is None:
        return _json_response({"error": "approval not found"}, status_code=404)

    from kmcp.studio import approvals

    ok = approvals.approve(db, approval_id=approval_id, approved_by=actor, status=decision)
    if not ok:
        return _json_response(
            {"error": "approval is not pending (already decided or consumed)"},
            status_code=409,
        )
    _audit(
        db,
        f"approval.{decision}",
        actor,
        entity_type="approval",
        entity_id=approval_id,
        details={"gate": row["gate"], "resource_id": row["resource_id"]},
    )
    return _json_response({"approval_id": approval_id, "status": decision})


async def list_approvals_api(request: Request) -> JSONResponse:
    """List approval requests (newest first) for operator review."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    try:
        limit = _parse_int(request.query_params.get("limit"), "limit", 100, minimum=1)
        offset = _parse_int(request.query_params.get("offset"), "offset", 0)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    cur = db.conn.cursor()
    rows = cur.execute(
        "SELECT * FROM pending_approvals ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return _json_response({"approvals": [dict(r) for r in rows], "limit": limit, "offset": offset})


async def list_versions(request: Request) -> JSONResponse:
    """List playbook versions."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    playbook_id = request.path_params["id"]
    versions = db.list_playbook_versions(playbook_id)
    return _json_response({"versions": versions})


async def rollback_playbook(request: Request) -> JSONResponse:
    """Rollback playbook to a previous version."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    playbook_id = request.path_params["id"]
    body = await request.json()
    version_id = body.get("version_id")

    from kmcp.studio import playbook_service

    try:
        playbook_service.rollback_version(db, playbook_id, version_id)
    except (ValueError, LookupError) as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    _audit(
        db,
        "playbook.rolled_back",
        _request_actor(request),
        entity_type="playbook",
        entity_id=playbook_id,
        details={"version_id": str(version_id)},
    )

    return _json_response({"status": "rolled_back", "version_id": str(version_id)})


async def export_playbook(request: Request) -> JSONResponse:
    """Export playbook."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    playbook_id = request.path_params["id"]
    from kmcp.studio import playbook_service

    pkg = playbook_service.export_playbook(db, playbook_id)
    if pkg is None:
        return _json_response({"error": "playbook not found"}, status_code=404)

    return _json_response(pkg)


async def import_playbook(request: Request) -> JSONResponse:
    """Import playbook."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    body = await request.json()
    package = body.get("package")
    if not package:
        return _json_response({"error": "package is required"}, status_code=422)

    from kmcp.studio import playbook_service

    result = playbook_service.import_playbook(db, package)
    _audit(
        db,
        "playbook.imported",
        _request_actor(request),
        entity_type="playbook",
        entity_id=result.get("playbook_id") if isinstance(result, dict) else None,
        details=result if isinstance(result, dict) else {},
    )
    return _json_response(result)


async def delete_playbook(request: Request) -> JSONResponse:
    """Delete playbook."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    playbook_id = request.path_params["id"]
    from kmcp.studio import playbook_service

    ok = playbook_service.delete_playbook(db, playbook_id)
    if not ok:
        return _json_response({"error": "playbook not found"}, status_code=404)
    _audit(
        db,
        "playbook.deleted",
        _request_actor(request),
        entity_type="playbook",
        entity_id=playbook_id,
    )

    return _json_response({"status": "deleted"})


async def seed_playbooks_api(request: Request) -> JSONResponse:
    """Seed built-in YAML playbooks into the DB."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    from kmcp.studio import playbook_service

    count = playbook_service.seed_builtin_playbooks(db)
    _audit(
        db,
        "playbooks.seeded",
        _request_actor(request),
        entity_type="playbook",
        details={"seeded": count},
    )
    return _json_response({"seeded": count})


async def create_playbook_api(request: Request) -> JSONResponse:
    """Create a new playbook."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    body = await request.json()
    playbook_id = body.get("playbook_id")
    name = body.get("name")
    if not playbook_id or not name:
        return _json_response({"error": "playbook_id and name required"}, status_code=400)
    from kmcp.studio import playbook_service

    try:
        row = playbook_service.create_playbook(
            db,
            playbook_id,
            name,
            description=body.get("description"),
            category=body.get("category", "custom"),
        )
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    _audit(
        db,
        "playbook.created",
        _request_actor(request),
        entity_type="playbook",
        entity_id=playbook_id,
    )
    return _json_response(row, status_code=201)


# ══════════════════════════════════════════════════════════════════════════════
# Execution routes
# ══════════════════════════════════════════════════════════════════════════════


async def list_executions(request: Request) -> JSONResponse:
    """List workflow executions."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    from kmcp.studio import service_executions

    status = request.query_params.get("status")
    playbook_id = request.query_params.get("playbook_id")
    try:
        limit = _parse_int(request.query_params.get("limit"), "limit", 50, minimum=1)
        offset = _parse_int(request.query_params.get("offset"), "offset", 0)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)

    rows = service_executions.list_executions(
        db, status=status, playbook_id=playbook_id, limit=limit, offset=offset
    )
    return _json_response({"executions": rows, "limit": limit, "offset": offset})


async def start_execution(request: Request) -> JSONResponse:
    """Start a playbook execution."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    from kmcp.studio import service_executions

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid JSON"}, status_code=400)

    playbook_id = body.get("playbook_id")
    inputs = body.get("inputs")
    debug = body.get("debug", False)
    trigger = body.get("trigger", "studio")

    try:
        result = await service_executions.start_execution(
            db, playbook_id, inputs=inputs, debug=debug, trigger=trigger
        )
    except (LookupError, ValueError) as exc:
        return _json_response({"error": str(exc)}, status_code=400)

    _audit(
        db,
        "execution.started",
        _request_actor(request),
        entity_type="execution",
        entity_id=result.get("execution", {}).get("execution_id")
        if isinstance(result, dict)
        else None,
        details={"playbook_id": playbook_id},
    )
    return _json_response(result)


async def get_execution(request: Request) -> JSONResponse:
    """Get execution detail."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    from kmcp.studio import service_executions

    execution_id = request.path_params["id"]
    row = service_executions.get_execution(db, execution_id)
    if row is None:
        return _json_response({"error": "execution not found"}, status_code=404)
    return _json_response({"execution": row})


async def cancel_execution(request: Request) -> JSONResponse:
    """Cancel execution."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    from kmcp.studio import service_executions

    execution_id = request.path_params["id"]
    try:
        await service_executions.cancel_execution(db, execution_id)
    except Exception as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    _audit(
        db,
        "execution.cancelled",
        _request_actor(request),
        entity_type="execution",
        entity_id=execution_id,
    )

    return _json_response({"status": "cancelled"})


async def resume_execution(request: Request) -> JSONResponse:
    """Resume execution."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    from kmcp.studio import service_executions

    execution_id = request.path_params["id"]
    try:
        result = await service_executions.resume_execution(db, execution_id)
    except (LookupError, ValueError) as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return _json_response({"error": f"resume failed: {exc}"}, status_code=500)
    if result is None:
        return _json_response({"error": "execution not found"}, status_code=404)
    _audit(
        db,
        "execution.resumed",
        _request_actor(request),
        entity_type="execution",
        entity_id=execution_id,
        details={"status": result.get("status")},
    )
    return _json_response(result)


# ══════════════════════════════════════════════════════════════════════════════
# mount_studio_routes — mounts all studio routes on a Starlette app
# ══════════════════════════════════════════════════════════════════════════════


async def server_info(request: Request) -> JSONResponse:
    """Return server info for the UI."""
    from pathlib import Path

    db_path = Path(config.db_path)
    db_exists = db_path.exists()
    info = {
        "db_path": str(db_path),
        "db_exists": db_exists,
        "server_name": getattr(config, "server_name", "kmcp"),
        "server_version": getattr(config, "server_version", "2.0.0"),
        "host": getattr(config, "host", "127.0.0.1"),
        "port": getattr(config, "port", 8399),
        "transport": getattr(config, "transport", "streamable-http"),
        "auth_enabled": bool(getattr(config, "api_token", ""))
        and not getattr(config, "insecure", False),
        "default_timeout": getattr(config, "default_timeout", 300),
        "max_timeout": getattr(config, "max_timeout", 600),
        "max_concurrent": getattr(config, "max_concurrent", 10),
        "debug": _debug_runtime["debug"],
    }
    health = _server_health()
    if health:
        for key in (
            "uptime_seconds",
            "active_executions",
            "total_requests",
            "total_errors",
            "memory_mb",
        ):
            if health.get(key) is not None:
                info[key] = health[key]
    return _json_response(info)


async def put_server_debug(request: Request) -> JSONResponse:
    """Toggle debug mode at runtime. Body: { "debug": <bool> }."""
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid JSON"}, status_code=400)
    _debug_runtime["debug"] = bool(body.get("debug", False))
    db = db_manager.get_db()
    if db is not None:
        _audit(
            db,
            "server.debug_changed",
            _request_actor(request),
            entity_type="server",
            entity_id="debug",
            details={"debug": _debug_runtime["debug"]},
        )
    return _json_response({"debug": _debug_runtime["debug"], "ok": True})


async def server_health_api(request: Request) -> JSONResponse:
    """Server health / uptime for the Overview page."""
    health = _server_health() or {}
    return _json_response(
        {
            "status": "ok",
            "uptime_seconds": health.get("uptime_seconds"),
            "active_executions": health.get("active_executions"),
            "total_requests": health.get("total_requests"),
            "total_errors": health.get("total_errors"),
            "memory_mb": health.get("memory_mb"),
            "debug": _debug_runtime["debug"],
        }
    )


async def list_connections_api(request: Request) -> JSONResponse:
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    conns = [_sanitize_connection(c) for c in service_connections.list_connections(db)]
    return _json_response({"connections": conns, "count": len(conns)})


async def upsert_connection_api(request: Request) -> JSONResponse:
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    body = await request.json()
    connection_id = body.get("connection_id")
    name = body.get("name")
    transport = body.get("transport")
    if not connection_id or not name or not transport:
        return _json_response({"error": "connection_id, name, transport required"}, status_code=400)
    row = service_connections.upsert_connection(
        db,
        connection_id=connection_id,
        name=name,
        transport=transport,
        description=body.get("description"),
        endpoint_url=body.get("endpoint_url"),
        command=body.get("command"),
        args=body.get("args"),
        auth_type=body.get("auth_type"),
        auth_config=body.get("auth_config"),
        enabled=body.get("enabled", True),
    )
    _audit(
        db,
        "connection.upserted",
        _request_actor(request),
        entity_type="connection",
        entity_id=connection_id,
        details={"auth_type": body.get("auth_type")},
    )
    return _json_response(_sanitize_connection(row), status_code=201)


async def get_connection_api(request: Request) -> JSONResponse:
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    connection_id = request.path_params["id"]
    cur = db.conn.cursor()
    row = cur.execute(
        "SELECT * FROM mcp_connections WHERE connection_id = ?", (connection_id,)
    ).fetchone()
    if row is None:
        return _json_response({"error": "connection not found"}, status_code=404)
    c = dict(row)
    for field in ("args", "auth_config"):
        raw = c.get(field)
        if raw:
            try:
                c[field] = json.loads(raw)
            except (TypeError, ValueError):
                pass
    cur.execute(
        "SELECT tool_id, visibility FROM tool_definitions WHERE provider = ?",
        (connection_id,),
    )
    imported_tools = [{"tool_id": r[0], "visibility": r[1]} for r in cur.fetchall()]
    c["imported_tools"] = imported_tools
    c["imported_tool_count"] = len(imported_tools)
    return _json_response({"connection": _sanitize_connection(c)})


async def delete_connection_api(request: Request) -> JSONResponse:
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    connection_id = request.path_params["id"]
    deleted = service_connections.delete_connection(db, connection_id)
    if not deleted:
        return _json_response({"error": "connection not found"}, status_code=404)
    _audit(
        db,
        "connection.deleted",
        _request_actor(request),
        entity_type="connection",
        entity_id=connection_id,
    )
    return _json_response({"status": "deleted"})


async def put_connection_api(request: Request) -> JSONResponse:
    """Update an existing MCP connection (partial update; omitted fields keep current values)."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    connection_id = request.path_params["id"]
    cur = db.conn.cursor()
    row = cur.execute(
        "SELECT * FROM mcp_connections WHERE connection_id = ?", (connection_id,)
    ).fetchone()
    if row is None:
        return _json_response({"error": "connection not found"}, status_code=404)
    existing = dict(row)
    for field in ("args", "auth_config"):
        raw = existing.get(field)
        if isinstance(raw, str):
            try:
                existing[field] = json.loads(raw)
            except (TypeError, ValueError):
                pass
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid JSON"}, status_code=400)
    updated = service_connections.upsert_connection(
        db,
        connection_id=connection_id,
        name=body.get("name", existing.get("name")),
        transport=body.get("transport", existing.get("transport")),
        description=body.get("description", existing.get("description")),
        endpoint_url=body.get("endpoint_url", existing.get("endpoint_url")),
        command=body.get("command", existing.get("command")),
        args=body.get("args", existing.get("args")),
        auth_type=body.get("auth_type", existing.get("auth_type")),
        auth_config=body.get("auth_config", existing.get("auth_config")),
        enabled=body.get("enabled", bool(existing.get("enabled", 1))),
    )
    _audit(
        db,
        "connection.updated",
        _request_actor(request),
        entity_type="connection",
        entity_id=connection_id,
    )
    return _json_response(_sanitize_connection(updated))


async def test_connection_api(request: Request) -> JSONResponse:
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    connection_id = request.path_params["id"]
    cur = db.conn.cursor()
    row = cur.execute(
        "SELECT transport, endpoint_url, command FROM mcp_connections WHERE connection_id = ?",
        (connection_id,),
    ).fetchone()
    if row is None:
        return _json_response({"error": "connection not found"}, status_code=404)
    transport, endpoint_url, command = row
    from kmcp.studio.mcp_client import ExternalMCPClient

    client = ExternalMCPClient(
        transport,
        url=endpoint_url,
        command=command.split() if command else None,
    )
    result = await service_mcp_connections.test_connection(client)
    ok = bool(result.get("ok"))
    service_connections.set_health(db, connection_id, ok)
    _audit(
        db,
        "connection.tested",
        _request_actor(request),
        entity_type="connection",
        entity_id=connection_id,
        details={"ok": ok, "error": result.get("error")},
    )
    return _json_response(result)


async def discover_connection_api(request: Request) -> JSONResponse:
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    connection_id = request.path_params["id"]
    cur = db.conn.cursor()
    row = cur.execute(
        "SELECT transport, endpoint_url, command FROM mcp_connections WHERE connection_id = ?",
        (connection_id,),
    ).fetchone()
    if row is None:
        return _json_response({"error": "connection not found"}, status_code=404)
    transport, endpoint_url, command = row
    from kmcp.studio.mcp_client import ExternalMCPClient

    client = ExternalMCPClient(
        transport,
        url=endpoint_url,
        command=command.split() if command else None,
    )
    try:
        imported = await service_mcp_connections.discover_and_import(
            db,
            connection_id,
            client,
            visibility="internal",
        )
    except Exception:
        service_connections.set_health(db, connection_id, False)
        raise
    service_connections.set_health(db, connection_id, True)
    _audit(
        db,
        "connection.discovered",
        _request_actor(request),
        entity_type="connection",
        entity_id=connection_id,
        details={"count": len(imported)},
    )
    return _json_response({"imported_tools": imported, "count": len(imported)})


async def overview_api(request: Request) -> JSONResponse:
    """Aggregate dashboard overview stats from across the studio."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)

    cur = db.conn.cursor()

    # Playbooks
    pb_rows = db_manager.DBManager.list_playbooks(db)
    pb_by_status: dict[str, int] = {}
    for r in pb_rows:
        st = r.get("status") or "unknown"
        pb_by_status[st] = pb_by_status.get(st, 0) + 1

    # Executions
    from kmcp.studio import service_executions

    exec_rows = service_executions.list_executions(db, limit=500)
    ex_by_status: dict[str, int] = {}
    for r in exec_rows:
        st = r.get("status") or "unknown"
        ex_by_status[st] = ex_by_status.get(st, 0) + 1
    recent_executions = [
        {
            "execution_id": r.get("execution_id"),
            "playbook_id": r.get("playbook_id"),
            "status": r.get("status"),
            "started_at": r.get("started_at"),
        }
        for r in exec_rows[:10]
    ]

    # Tools
    td_rows = db.list_tool_definitions()
    td_by_visibility: dict[str, int] = {}
    for r in td_rows:
        vis = r.get("visibility") or "unknown"
        td_by_visibility[vis] = td_by_visibility.get(vis, 0) + 1

    # Connections
    conn_rows = service_connections.list_connections(db)
    conn_healthy = sum(1 for c in conn_rows if c.get("health_status") == "healthy")

    # Agents
    try:
        agent_rows = db.list_agent_profiles()
    except Exception:
        agent_rows = []

    # Findings
    findings_total = 0
    try:
        ft = cur.execute("SELECT COALESCE(SUM(findings_count), 0) FROM scans").fetchone()[0]
        findings_total = int(ft or 0)
    except Exception:
        findings_total = 0

    try:
        finding_rows = cur.execute(
            "SELECT tool, command, findings_count, started_at FROM scans ORDER BY started_at DESC LIMIT 10"
        ).fetchall()
        recent_findings = [
            {
                "port_state": r[0] or "—",
                "tool": r[1] or "—",
                "severity": "info",
                "test_name": f"scan ({r[2] or 0} findings)",
            }
            for r in finding_rows
        ]
    except Exception:
        recent_findings = []

    return _json_response(
        {
            "playbooks": {"total": len(pb_rows), "by_status": pb_by_status},
            "executions": {"total": len(exec_rows), "by_status": ex_by_status},
            "tools": {"total": len(td_rows), "by_visibility": td_by_visibility},
            "findings": {"total": findings_total, "by_severity": {}},
            "connections": {"total": len(conn_rows), "healthy": conn_healthy},
            "agents": {"total": len(agent_rows)},
            "recent_executions": recent_executions,
            "recent_findings": recent_findings,
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tool Registry handlers
# ══════════════════════════════════════════════════════════════════════════════


async def list_tools_api(request: Request) -> JSONResponse:
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    try:
        limit = _parse_int(request.query_params.get("limit"), "limit", 500, minimum=1)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    visibility = request.query_params.get("visibility")
    enabled = request.query_params.get("enabled")
    enabled_bool = None
    if enabled is not None:
        enabled_bool = enabled.lower() == "true"
    rows = db.list_tool_definitions(
        visibility=visibility,
        enabled=enabled_bool,
    )
    tools = []
    for r in rows[:limit]:
        schema = r["input_schema"]
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except (TypeError, ValueError):
                schema = {}
        tools.append(
            {
                "tool_id": r["tool_id"],
                "name": r["name"],
                "description": r["description"],
                "provider": r["provider"],
                "visibility": r["visibility"],
                "enabled": bool(r["enabled"]),
                "input_schema": schema,
                "default_timeout": r.get("default_timeout"),
                "alias_for": r.get("alias_for"),
            }
        )
    return _json_response({"tools": tools, "count": len(tools)})


async def set_tool_visibility_api(request: Request) -> JSONResponse:
    """Set a tool's visibility mode in the DB and sync the runtime registry."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    tool_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid JSON"}, status_code=400)
    visibility = body.get("visibility")
    # Promoting a tool to direct agent use is a privileged change (BUG-021).
    # Demotions are always allowed so operators can immediately narrow access.
    if visibility == "agent_callable":
        from kmcp.studio import approvals

        if not approvals.is_approved(db, approvals.GATE_VISIBILITY_AGENT_CALLABLE, tool_id):
            return _json_response(
                {"error": approvals.require_approval_for(approvals.GATE_VISIBILITY_AGENT_CALLABLE)},
                status_code=403,
            )
        approvals.consume_approval(db, approvals.GATE_VISIBILITY_AGENT_CALLABLE, tool_id)
    try:
        ok = service_tools.set_tool_visibility(db, tool_id, visibility)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    if not ok:
        return _json_response({"error": "tool not found"}, status_code=404)
    _audit(
        db,
        "tool.visibility_changed",
        _request_actor(request),
        entity_type="tool",
        entity_id=tool_id,
        details={"visibility": visibility},
    )
    return _json_response({"tool_id": tool_id, "visibility": visibility})


async def run_tool_api(request: Request) -> JSONResponse:
    """Run a single registered tool once via its ``safe_execute`` wrapper.

    Used by the Studio "Test tool" action. Resolves aliases (e.g. ``search``
    -> ``searchsploit``) before executing.
    """
    tool_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid JSON"}, status_code=400)
    args = body.get("args", {})
    if not isinstance(args, dict):
        return _json_response({"error": "args must be an object"}, status_code=400)
    from kmcp.registry import registry as global_registry

    tool = global_registry.get(tool_id)
    if tool is None:
        return _json_response({"error": "tool not found"}, status_code=404)
    try:
        result = await tool.safe_execute(args)
    except Exception as exc:  # pragma: no cover - safe_execute catches tool errors
        return _json_response(
            {"error": f"tool run failed: {redaction_service.redact_log(str(exc))}"},
            status_code=500,
        )
    db = db_manager.get_db()
    if db is not None:
        _audit(
            db,
            "tool.executed",
            _request_actor(request),
            entity_type="tool",
            entity_id=tool_id,
        )
    return _json_response({"tool_id": tool_id, "result": result})


#: Maximum number of rows considered when the caller asks for global sorting.
#: Sorting is applied in the API layer because the DB query API only supports
#: filtering and pagination; the cap keeps an explicit bound on memory use.
_FINDING_SORT_FETCH_LIMIT = 10000
_FINDING_SORT_FIELDS = frozenset({"title", "severity", "tool", "target", "status", "created_at"})


def _finding_sort_key(row: dict, field: str) -> str:
    """Return a stable string sort key for a finding row."""
    if field == "title":
        value = row.get("title") or row.get("test_name") or row.get("checkname") or row.get("name")
    elif field == "status":
        value = row.get("status") or row.get("validation_status")
    else:
        value = row.get(field)
    return str(value or "")


async def findings_api(request: Request) -> JSONResponse:
    """Query normalized findings with filters, global sort and pagination.

    Pagination is performed exactly once, here. The frontend renders the page
    returned by this endpoint as-is instead of slicing it a second time.
    """
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    target = request.query_params.get("target")
    severity = request.query_params.get("severity")
    tool = request.query_params.get("tool")
    try:
        limit = _parse_int(request.query_params.get("limit"), "limit", 100, minimum=1)
        offset = _parse_int(request.query_params.get("offset"), "offset", 0)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)

    sort = request.query_params.get("sort")
    direction = request.query_params.get("dir", "asc").lower()
    if sort and sort not in _FINDING_SORT_FIELDS:
        return _json_response({"error": "invalid sort field"}, status_code=400)
    if direction not in ("asc", "desc"):
        return _json_response({"error": "invalid sort direction"}, status_code=400)

    if sort:
        # Fetch the filtered set once, sort globally, then paginate locally.
        result = db.query_findings(
            target=target,
            severity=severity,
            tool=tool,
            limit=_FINDING_SORT_FETCH_LIMIT,
            offset=0,
        )
        rows = sorted(
            result.get("findings", []),
            key=lambda r: _finding_sort_key(r, sort),
            reverse=direction == "desc",
        )
        result = {
            "count": result.get("count", 0),
            "findings": rows[offset : offset + limit],
            "sort": sort,
            "dir": direction,
            "truncated": result.get("count", 0) > _FINDING_SORT_FETCH_LIMIT,
        }
    else:
        result = db.query_findings(
            target=target,
            severity=severity,
            tool=tool,
            limit=limit,
            offset=offset,
        )
    return _json_response(result)


async def update_finding_api(request: Request) -> JSONResponse:
    """Update a finding's status (e.g. open / resolved)."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    try:
        finding_id = _parse_int(request.path_params.get("id"), "finding_id", 0, minimum=1)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid JSON"}, status_code=400)
    status = body.get("status")
    if not status:
        return _json_response({"error": "status is required"}, status_code=400)
    ok = db.update_finding_status(finding_id, status)
    if not ok:
        return _json_response({"error": "finding not found"}, status_code=404)
    _audit(
        db,
        "finding.updated",
        _request_actor(request),
        entity_type="finding",
        entity_id=finding_id,
        details={"status": status},
    )
    return _json_response({"status": "updated", "id": finding_id, "finding_status": status})


async def delete_finding_api(request: Request) -> JSONResponse:
    """Delete a finding."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    try:
        finding_id = _parse_int(request.path_params.get("id"), "finding_id", 0, minimum=1)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    ok = db.delete_finding(finding_id)
    if not ok:
        return _json_response({"error": "finding not found"}, status_code=404)
    _audit(
        db,
        "finding.deleted",
        _request_actor(request),
        entity_type="finding",
        entity_id=finding_id,
    )
    return _json_response({"status": "deleted", "id": finding_id})


# ══════════════════════════════════════════════════════════════════════════════
# Agent Profiles routes
# ══════════════════════════════════════════════════════════════════════════════


async def list_agents_api(request: Request) -> JSONResponse:
    """List all agent profiles."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    rows = service_agents.list_profiles(db)
    agents = []
    for r in rows:
        agents.append(
            {
                "profile_id": r.get("profile_id"),
                "name": r.get("name"),
                "description": r.get("description"),
                "default_visibility": r.get("default_visibility"),
                "max_parallel_operations": r.get("max_parallel_operations"),
                "max_execution_seconds": r.get("max_execution_seconds"),
            }
        )
    return _json_response({"agents": agents})


async def create_agent_api(request: Request) -> JSONResponse:
    """Create an agent profile."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    body = await request.json()
    profile_id = body.get("profile_id")
    name = body.get("name")
    if not profile_id or not name:
        return _json_response({"error": "profile_id and name required"}, status_code=400)
    try:
        service_agents.create_profile(
            db,
            profile_id,
            name,
            description=body.get("description"),
        )
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    _audit(
        db,
        "agent.created",
        _request_actor(request),
        entity_type="agent_profile",
        entity_id=profile_id,
    )
    return _json_response(service_agents.get_profile(db, profile_id), status_code=201)


async def get_agent_api(request: Request) -> JSONResponse:
    """Get agent profile with effective catalog, permissions and assignments."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    profile_id = request.path_params["id"]
    agent = service_agents.get_profile(db, profile_id)
    if agent is None:
        return _json_response({"error": "agent profile not found"}, status_code=404)
    catalog = service_agents.effective_catalog(db, profile_id)
    permissions = service_agents.list_tool_permission_rows(db, profile_id)
    for p in permissions:
        p["profile_id"] = profile_id
    assignments = service_agents.list_playbook_assignments(db, profile_id)
    for a in assignments:
        a["profile_id"] = profile_id
    return _json_response(
        {
            "agent": agent,
            "effective_catalog": catalog,
            "permissions": permissions,
            "assignments": assignments,
        }
    )


async def update_agent_api(request: Request) -> JSONResponse:
    """Update an agent profile's name and/or description (partial update)."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    profile_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid JSON"}, status_code=400)
    name = body.get("name")
    description = body.get("description")
    if name is None and description is None:
        return _json_response({"error": "name or description required"}, status_code=400)
    ok = service_agents.update_profile(db, profile_id, name=name, description=description)
    if not ok:
        return _json_response({"error": "agent profile not found"}, status_code=404)
    _audit(
        db,
        "agent.updated",
        _request_actor(request),
        entity_type="agent_profile",
        entity_id=profile_id,
    )
    return _json_response(service_agents.get_profile(db, profile_id))


async def delete_agent_api(request: Request) -> JSONResponse:
    """Delete an agent profile."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    profile_id = request.path_params["id"]
    deleted = service_agents.delete_profile(db, profile_id)
    if not deleted:
        return _json_response({"error": "agent profile not found"}, status_code=404)
    _audit(
        db,
        "agent.deleted",
        _request_actor(request),
        entity_type="agent_profile",
        entity_id=profile_id,
    )
    return _json_response({"status": "deleted"})


async def grant_agent_tool_api(request: Request) -> JSONResponse:
    """Grant a tool to an agent profile."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    profile_id = request.path_params["id"]
    tool_id = request.path_params["tool_id"]
    service_agents.grant_tool(db, profile_id, tool_id, granted=True)
    _audit(
        db,
        "agent.tool_granted",
        _request_actor(request),
        entity_type="agent_profile",
        entity_id=profile_id,
        details={"tool_id": tool_id},
    )
    return _json_response({"profile_id": profile_id, "tool_id": tool_id, "granted": True})


async def revoke_agent_tool_api(request: Request) -> JSONResponse:
    """Revoke a tool from an agent profile."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    profile_id = request.path_params["id"]
    tool_id = request.path_params["tool_id"]
    service_agents.revoke_tool(db, profile_id, tool_id)
    _audit(
        db,
        "agent.tool_revoked",
        _request_actor(request),
        entity_type="agent_profile",
        entity_id=profile_id,
        details={"tool_id": tool_id},
    )
    return _json_response({"profile_id": profile_id, "tool_id": tool_id, "revoked": True})


async def assign_agent_playbook_api(request: Request) -> JSONResponse:
    """Assign a playbook to an agent profile."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    profile_id = request.path_params["id"]
    playbook_id = request.path_params["playbook_id"]
    service_agents.assign_playbook(db, profile_id, playbook_id, granted=True)
    _audit(
        db,
        "agent.playbook_assigned",
        _request_actor(request),
        entity_type="agent_profile",
        entity_id=profile_id,
        details={"playbook_id": playbook_id},
    )
    return _json_response({"profile_id": profile_id, "playbook_id": playbook_id, "assigned": True})


async def unassign_agent_playbook_api(request: Request) -> JSONResponse:
    """Unassign a playbook from an agent profile."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    profile_id = request.path_params["id"]
    playbook_id = request.path_params["playbook_id"]
    service_agents.unassign_playbook(db, profile_id, playbook_id)
    _audit(
        db,
        "agent.playbook_unassigned",
        _request_actor(request),
        entity_type="agent_profile",
        entity_id=profile_id,
        details={"playbook_id": playbook_id},
    )
    return _json_response(
        {"profile_id": profile_id, "playbook_id": playbook_id, "unassigned": True}
    )


async def audit_api(request: Request) -> JSONResponse:
    """List append-only Studio audit records (newest first)."""
    db = db_manager.get_db()
    if db is None:
        return _json_response({"error": "database unavailable"}, status_code=500)
    try:
        limit = _parse_int(request.query_params.get("limit"), "limit", 100, minimum=1)
        offset = _parse_int(request.query_params.get("offset"), "offset", 0)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    rows = audit_service.list_audit(
        db,
        event=request.query_params.get("event"),
        entity_type=request.query_params.get("entity_type"),
        actor=request.query_params.get("actor"),
        limit=limit,
        offset=offset,
    )
    return _json_response({"audit": rows, "limit": limit, "offset": offset})


def mount_studio_routes(app) -> None:
    """Mount all Studio REST routes on the given Starlette app."""

    # Playbook routes
    app.add_route("/api/playbooks", list_playbooks, methods=["GET"])
    app.add_route("/api/playbooks", import_playbook, methods=["POST"])
    app.add_route("/api/playbooks/import", import_playbook, methods=["POST"])
    app.add_route("/api/playbooks/create", create_playbook_api, methods=["POST"])
    app.add_route("/api/playbooks/seed", seed_playbooks_api, methods=["POST"])
    app.add_route("/api/playbooks/{id}", get_playbook, methods=["GET"])
    app.add_route("/api/playbooks/{id}", delete_playbook, methods=["DELETE"])
    app.add_route("/api/playbooks/{id}/definition", put_definition, methods=["PUT"])
    app.add_route("/api/playbooks/{id}/definition", get_definition_api, methods=["GET"])
    app.add_route("/api/playbooks/{id}/publish", put_definition_publish, methods=["POST"])
    app.add_route("/api/playbooks/{id}/approval-request", approval_request_api, methods=["POST"])
    app.add_route(
        "/api/playbooks/{id}/validate-definition", validate_definition_api, methods=["POST"]
    )
    app.add_route("/api/playbooks/{id}/versions", list_versions, methods=["GET"])
    app.add_route("/api/playbooks/{id}/rollback", rollback_playbook, methods=["POST"])
    app.add_route("/api/playbooks/{id}/export", export_playbook, methods=["GET"])

    # Approval decisions (bearer-token authenticated via TokenAuthMiddleware)
    app.add_route("/api/approvals", list_approvals_api, methods=["GET"])
    app.add_route("/api/approvals/{id}/approve", decide_approval_api, methods=["POST"])
    app.add_route("/api/approvals/{id}/reject", decide_approval_api, methods=["POST"])

    # Append-only audit trail
    app.add_route("/api/audit", audit_api, methods=["GET"])

    # Server info
    app.add_route("/api/server", server_info, methods=["GET"])
    app.add_route("/api/server/debug", put_server_debug, methods=["PUT"])
    app.add_route("/api/server/health", server_health_api, methods=["GET"])

    # Overview
    app.add_route("/api/overview", overview_api, methods=["GET"])

    # MCP Connections routes
    app.add_route("/api/connections", list_connections_api, methods=["GET"])
    app.add_route("/api/connections", upsert_connection_api, methods=["POST"])
    app.add_route("/api/connections/{id}", get_connection_api, methods=["GET"])
    app.add_route("/api/connections/{id}", put_connection_api, methods=["PUT"])
    app.add_route("/api/connections/{id}", delete_connection_api, methods=["DELETE"])
    app.add_route("/api/connections/{id}/test", test_connection_api, methods=["POST"])
    app.add_route("/api/connections/{id}/discover", discover_connection_api, methods=["POST"])

    # Tool Registry routes
    app.add_route("/api/tools", list_tools_api, methods=["GET"])
    app.add_route("/api/tools/{id}/visibility", set_tool_visibility_api, methods=["PUT"])
    app.add_route("/api/tools/{id}/run", run_tool_api, methods=["POST"])

    # Findings routes
    app.add_route("/api/findings", findings_api, methods=["GET"])
    app.add_route("/api/findings/{id}", update_finding_api, methods=["PUT"])
    app.add_route("/api/findings/{id}", delete_finding_api, methods=["DELETE"])

    # Agent Profiles routes
    app.add_route("/api/agents", list_agents_api, methods=["GET"])
    app.add_route("/api/agents", create_agent_api, methods=["POST"])
    app.add_route("/api/agents/{id}", get_agent_api, methods=["GET"])
    app.add_route("/api/agents/{id}", update_agent_api, methods=["PUT"])
    app.add_route("/api/agents/{id}", delete_agent_api, methods=["DELETE"])
    app.add_route("/api/agents/{id}/tools/{tool_id}", grant_agent_tool_api, methods=["PUT"])
    app.add_route("/api/agents/{id}/tools/{tool_id}", revoke_agent_tool_api, methods=["DELETE"])
    app.add_route(
        "/api/agents/{id}/playbooks/{playbook_id}", assign_agent_playbook_api, methods=["PUT"]
    )
    app.add_route(
        "/api/agents/{id}/playbooks/{playbook_id}", unassign_agent_playbook_api, methods=["DELETE"]
    )

    # Execution routes
    app.add_route("/api/executions", list_executions, methods=["GET"])
    app.add_route("/api/executions", start_execution, methods=["POST"])
    app.add_route("/api/executions/{id}", get_execution, methods=["GET"])
    app.add_route("/api/executions/{id}/cancel", cancel_execution, methods=["POST"])
    app.add_route("/api/executions/{id}/resume", resume_execution, methods=["POST"])
