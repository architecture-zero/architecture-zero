"""User administration: accounts, roles, departments, permissions, unlock.

Sixth router out of main.py. Same rules: no prefix, full literal paths, guards
verbatim on the handlers, never `from app.main import ...`.

CreateUserRequest arrives here from the auth commit, which deliberately left it
behind: it sits between two auth models in the file, and POST /api/users is its
only consumer. This is where it belongs.

The function-local imports inside the handlers (is_owner, count_active_owners,
can_grant, IntegrityError) move verbatim and are NOT hoisted. count_active_owners
in particular is imported inside a branch of change_role - the last-owner latch -
and hoisting it would separate the import from the guard it exists for.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.jwt_auth import require_permission, hash_password, validate_password
from app.logger import log
from app.permissions import PERMISSION_SCOPES, ROLE_PERMISSIONS
from app.users import (create_user, list_users, deactivate_user, update_user_role,
                       update_user_department, update_user_permissions,
                       revoke_all_user_tokens, get_user_by_id, disable_mfa,
                       unlock_user)

router = APIRouter()


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "member"
    department: str = "general"


@router.get("/api/users")
def get_users(current_user: dict = Depends(require_permission("manage_users"))):
    # Strip secret material: a bcrypt hash is offline-crackable and the TOTP
    # secret clones the authenticator - neither belongs in an admin listing.
    safe = [{k: v for k, v in u.items() if k not in ("password_hash", "mfa_secret")}
            for u in list_users()]
    return {"users": safe}


@router.post("/api/users")
def add_user(request: CreateUserRequest, current_user: dict = Depends(require_permission("manage_users"))):
    from app.permissions import is_owner
    if request.role not in ("owner", "admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'owner', 'admin', or 'member'")
    # Only an Owner can mint another Owner - an Admin holds manage_users but
    # must not be able to escalate anyone (incl. itself) to superuser.
    if request.role == "owner" and not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Only an Owner can create an Owner")
    errors = validate_password(request.password)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    # users.username is UNIQUE, and a bare IntegrityError from the flush
    # surfaces as a 500 with a SQL traceback - an operator retyping an existing
    # name reads that as "the server is broken", not "pick another name".
    from sqlalchemy.exc import IntegrityError
    try:
        user_id = create_user(request.username, hash_password(request.password), role=request.role, department=request.department)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="That username is already taken")
    log("auth_create_user", admin_id=current_user["id"], new_user_id=user_id, username=request.username, department=request.department)
    return {"status": "created", "user_id": user_id}


@router.delete("/api/users/{user_id}")
def remove_user(user_id: int, current_user: dict = Depends(require_permission("manage_users"))):
    from app.permissions import is_owner
    from app.users import count_active_owners
    if user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    target = get_user_by_id(user_id)
    if target and target.get("role") == "owner":
        # Owner accounts are protected: only an Owner may deactivate an
        # Owner, and the LAST active Owner can never be removed - doing so
        # drops owner_exists() to false and re-opens the public
        # /api/auth/setup bootstrap to anyone (takeover).
        if not is_owner(current_user):
            raise HTTPException(status_code=403, detail="Only an Owner can deactivate an Owner")
        if count_active_owners() <= 1:
            raise HTTPException(status_code=400, detail="Cannot deactivate the last Owner")
    deactivate_user(user_id)
    revoke_all_user_tokens(user_id)
    log("auth_deactivate_user", admin_id=current_user["id"], target_user_id=user_id)
    return {"status": "deactivated"}


@router.patch("/api/users/{user_id}/role")
def change_role(user_id: int, body: dict, current_user: dict = Depends(require_permission("manage_users"))):
    from app.permissions import is_owner
    role = body.get("role")
    if role not in ("owner", "admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'owner', 'admin', or 'member'")
    # Granting Owner, or changing an existing Owner's role, is Owner-only -
    # an Admin must not be able to create a superuser or demote/lock out the
    # Owner.
    target = get_user_by_id(user_id)
    if (role == "owner" or (target and target.get("role") == "owner")) and not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Only an Owner can grant or change an Owner role")
    # Never demote the last Owner - it would orphan the system and re-open
    # public setup.
    if target and target.get("role") == "owner" and role != "owner":
        from app.users import count_active_owners
        if count_active_owners() <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last Owner")
    update_user_role(user_id, role)
    log("auth_change_role", admin_id=current_user["id"], target_user_id=user_id, role=role)
    return {"status": "updated"}


@router.patch("/api/users/{user_id}/department")
def change_department(user_id: int, body: dict, current_user: dict = Depends(require_permission("manage_users"))):
    dept = body.get("department", "general").strip() or "general"
    update_user_department(user_id, dept)
    log("auth_change_dept", admin_id=current_user["id"], target_user_id=user_id, department=dept)
    return {"status": "updated"}


@router.patch("/api/users/{user_id}/permissions")
def change_permissions(user_id: int, body: dict, current_user: dict = Depends(require_permission("manage_users"))):
    from app.permissions import can_grant
    target = get_user_by_id(user_id)
    perms = body.get("permissions")
    # Authority ceiling BEFORE any write - manage_users alone must not be a
    # route to manage_system (and from there to the provider keys in config).
    # Reset-to-defaults is checked too: it is still a write to the target's
    # authority, and an Owner's row stays Owner-managed.
    refusal = can_grant(current_user, target, perms or [])
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    if perms is None:
        # Reset to role defaults
        update_user_permissions(user_id, [])
    else:
        invalid = [p for p in perms if p not in PERMISSION_SCOPES]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Unknown scopes: {invalid}")
        update_user_permissions(user_id, perms)
    log("auth_change_permissions", admin_id=current_user["id"], target_user_id=user_id)
    return {"status": "updated"}


@router.post("/api/admin/users/{user_id}/mfa-reset")
def admin_mfa_reset(user_id: int, current_user: dict = Depends(require_permission("manage_users"))):
    """Admin: disable MFA for a user (e.g. lost authenticator).

    Owner targets are Owner-only. Stripping a principal's second factor is a
    write to THEIR authentication boundary, not to your own - an Admin who can
    do it to the Owner has turned an Owner takeover from "needs the password
    and the device" into "needs the password". change_role and can_grant both
    already refuse to let an Admin act on an Owner; this is the same rule on
    the authentication axis, which was the one path still missing it.
    """
    from app.permissions import is_owner
    target = get_user_by_id(user_id)
    if target and target.get("role") == "owner" and not is_owner(current_user):
        raise HTTPException(status_code=403,
                            detail="Only an Owner can reset an Owner's MFA")
    disable_mfa(user_id)
    log("admin_mfa_reset", admin_id=current_user["id"], target_user_id=user_id)
    return {"status": "MFA disabled"}


@router.post("/api/admin/users/{user_id}/unlock")
def admin_unlock_user(user_id: int, current_user: dict = Depends(require_permission("manage_users"))):
    """Admin: unlock a locked account."""
    unlock_user(user_id)
    log("admin_unlock_user", admin_id=current_user["id"], target_user_id=user_id)
    return {"status": "unlocked"}


@router.get("/api/admin/permissions")
def admin_get_permissions(current_user: dict = Depends(require_permission("manage_users"))):
    return {"scopes": PERMISSION_SCOPES, "presets": ROLE_PERMISSIONS}
