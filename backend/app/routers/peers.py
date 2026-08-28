"""Eco Mode peer sources - the registry side.

Third router out of main.py. Same rules: no prefix, full literal paths, guards
carried verbatim on the handlers, never `from app.main import ...`.

All six routes are require_owner today, and they still carry it individually
rather than as a router-level dependency. A shared dependency would read as the
same thing while silently granting owner to whatever seventh route lands here
next, and it would hide a downgrade from the level-aware pin in
test_route_authz_wiring.py.

The SERVE side of Eco Mode (`GET /api/query-kb`) deliberately stays in main:
it is public-by-design behind the X-Peer-Key middleware, and it shares nothing
with these six.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.jwt_auth import require_owner
from app.peers import (get_peers, save_peers, check_peer_health,
                       get_peers_with_health, reset_peer_circuit_breaker,
                       validate_peer_url, PeerURLRefused)

router = APIRouter()


# -- Peer AI Sources (Eco Mode) -----------------------------------------------

class PeerCreateRequest(BaseModel):
    id:      str
    name:    str
    url:     str
    model:   str = ""
    enabled: bool = True


class PeerUpdateRequest(BaseModel):
    name:    str | None = None
    url:     str | None = None
    model:   str | None = None
    enabled: bool | None = None


@router.get("/api/peers")
def list_peers_endpoint(current_user: dict = Depends(require_owner)):
    return {"peers": get_peers()}


@router.get("/api/peers/status")
def peers_status(current_user: dict = Depends(require_owner)):
    # Health-tracked view: the live probe stays, and the per-peer
    # failure/latency/breaker state rides along so a degraded peer is
    # diagnosable from the panel instead of from logs.
    peers = get_peers_with_health()
    results = []
    for p in peers:
        online = check_peer_health(p["url"]) if p.get("enabled") else None
        results.append({**p, "online": online})
    return {"peers": results}


@router.post("/api/peers/{peer_id}/reset-breaker")
def reset_peer_breaker(peer_id: str, current_user: dict = Depends(require_owner)):
    """Manually close a peer's circuit (e.g. right after fixing the peer)
    instead of waiting out the backoff window."""
    reset_peer_circuit_breaker(peer_id)
    return {"peer_id": peer_id, "circuit_open": False}


@router.post("/api/peers")
def add_peer(body: PeerCreateRequest, current_user: dict = Depends(require_owner)):
    # Refuse the SSRF shapes at write time so the operator gets a 400 at the
    # panel instead of a silent per-chat failure later. The fetch path
    # re-checks: this gate cannot see a name that re-resolves inward after it
    # was saved.
    try:
        url = validate_peer_url(body.url)
    except PeerURLRefused as e:
        raise HTTPException(status_code=400, detail=str(e))
    peers = [p for p in get_peers() if p.get("id") != body.id]
    peers.append({
        "id":      body.id,
        "name":    body.name,
        "url":     url,
        "model":   body.model,
        "enabled": body.enabled,
    })
    save_peers(peers)
    return {"peers": peers}


@router.patch("/api/peers/{peer_id}")
def update_peer(peer_id: str, body: PeerUpdateRequest, current_user: dict = Depends(require_owner)):
    if body.url is not None:
        try:
            body.url = validate_peer_url(body.url)
        except PeerURLRefused as e:
            raise HTTPException(status_code=400, detail=str(e))
    peers = get_peers()
    for p in peers:
        if p.get("id") == peer_id:
            if body.name    is not None: p["name"]    = body.name
            if body.url     is not None: p["url"]     = body.url
            if body.model   is not None: p["model"]   = body.model
            if body.enabled is not None: p["enabled"] = body.enabled
            break
    save_peers(peers)
    return {"peers": peers}


@router.delete("/api/peers/{peer_id}")
def delete_peer(peer_id: str, current_user: dict = Depends(require_owner)):
    peers = [p for p in get_peers() if p.get("id") != peer_id]
    save_peers(peers)
    return {"peers": peers}
