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
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from app.jwt_auth import (require_permission, hash_password, validate_password,
                          require_step_up, password_required_for_authority,
                          require_code_to_unlock_yourself, lock_minutes_left,
                          username_refusal)
from app.logger import log
from app.permissions import PERMISSION_SCOPES, ROLE_PERMISSIONS
from app.users import (create_user, list_users, deactivate_user, update_user_role,
                       update_user_department, update_user_permissions,
                       revoke_all_user_tokens, get_user_by_id, disable_mfa,
                       unlock_user, get_user_by_id_any_status)

router = APIRouter()


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "member"
    department: str = "general"
    # The CALLER's own password - the step-up. Optional at the schema so a
    # missing value is the route's 400 (a string detail), never pydantic's
    # 422 (a list detail the admin panel cannot render).
    current_password: str = ""


@router.get("/api/users")
def get_users(current_user: dict = Depends(require_permission("manage_users"))):
    # Strip secret material: a bcrypt hash is offline-crackable and the TOTP
    # secret clones the authenticator - neither belongs in an admin listing.
    safe = [{k: v for k, v in u.items() if k not in ("password_hash", "mfa_secret")}
            for u in list_users()]
    return {"users": safe}


def _refuse_create_beyond_creator(current_user: dict, role: str, department: str) -> None:
    """The create door's ceiling below the Owner (2026-10-31).

    With manage_users alone, a holder below the Owner created an account in
    ANY department with a password of its choosing, signed in as it and read
    that department through the chat - the widening the department change
    door was closed against, through the create door. So: the creator's own
    department or the shared one, compared as the collections the names key
    (database._collection_name, the KB write check's comparison) - an account
    in the shared department reads only what every account reads. And never
    a role above the creator's own rung (a Member holding manage_users minted
    an Admin), nor one whose preset holds a scope the creator does not (an
    Admin the Owner narrowed minted the preset it was denied): the permission
    door's rule, can_grant, on the role's preset."""
    from app.database import _collection_name
    from app.permissions import ROLE_LEVELS, can_grant, effective_level
    if ROLE_LEVELS[role] > effective_level(current_user):
        raise HTTPException(status_code=403, detail="Cannot create an account above your own role")
    refusal = can_grant(current_user, None, ROLE_PERMISSIONS[role])
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    allowed = {_collection_name("general"), _collection_name(current_user.get("department"))}
    if _collection_name(department) not in allowed:
        raise HTTPException(status_code=403,
                            detail="Only an Owner can create an account in another department")


@router.post("/api/users")
def add_user(request: CreateUserRequest, current_user: dict = Depends(require_permission("manage_users"))):
    from app.permissions import is_owner
    # STEP-UP FIRST (ruled 2026-09-06, applied fleet-wide 2026-09-09): a
    # stolen session must not mint an account that outlives it.
    # Re-authentication precedes the authority checks below, so a wrong
    # password learns nothing about the ceilings either.
    require_step_up(current_user, request.current_password, "create an account")
    # An account IN the reserved help department would have the product's
    # help pages merged into its NORMAL answers (2026-09-21) - a department
    # resolves through the same machinery. Refused at both department doors.
    from app.help_docs import refuse_reserved_department
    refuse_reserved_department(request.department)
    if request.role not in ("owner", "admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'owner', 'admin', or 'member'")
    # Only an Owner can mint another Owner - an Admin holds manage_users but
    # must not be able to escalate anyone (incl. itself) to superuser.
    if request.role == "owner" and not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Only an Owner can create an Owner")
    if not is_owner(current_user):
        _refuse_create_beyond_creator(current_user, request.role, request.department)
    refusal = username_refusal(request.username)
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)
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
    # Step-up first - see add_user.
    require_step_up(current_user, body.get("current_password") or "", "change a role")
    role = body.get("role")
    if role not in ("owner", "admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'owner', 'admin', or 'member'")
    # Your own authority is not yours to edit, in either direction (the forks'
    # shape, here since 2026-10-01 - the 09-22 review's residue).
    if user_id == current_user["id"]:
        raise HTTPException(status_code=403, detail="Cannot change your own role")
    # An id that names no ACTIVE account is a 404, before anything is written:
    # get_user_by_id reads active rows only and update_user_role writes by id
    # alone, so this answered "updated" for a missing id and wrote the role
    # onto a deactivated account.
    target = get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such user")
    # Granting Owner, or changing an existing Owner's role, is Owner-only -
    # an Admin must not be able to create a superuser or demote/lock out the
    # Owner.
    if (role == "owner" or target.get("role") == "owner") and not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Only an Owner can grant or change an Owner role")
    # An account with no usable password may not hold a role whose preset
    # carries manage_users or manage_system (upstream's SSO-account rule, ported
    # 2026-09-09; no such accounts exist on a fresh clone, and the rule is
    # here so an SSO port inherits it rather than rediscovers it).
    refusal = password_required_for_authority(target, ROLE_PERMISSIONS.get(role, []))
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)
    # Never demote the last Owner - it would orphan the system and re-open
    # public setup. Unreachable through this route while the self-check holds
    # (only another Owner may change an Owner's role); kept as the backstop.
    if target.get("role") == "owner" and role != "owner":
        from app.users import count_active_owners
        if count_active_owners() <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last Owner")
    # Setting the role an account already has changes nothing, so it writes
    # nothing - it used to reset the explicit list it was not changing (the
    # 09-22 review). After every guard, so a caller without the authority
    # still meets its refusal rather than an "unchanged".
    if target.get("role") == role:
        return {"status": "unchanged", "permissions_reset": False}
    # The role change resets any explicit permission list to the new role's
    # preset (users.update_user_role); the answer says so, so the caller knows
    # to grant any extra scope again.
    had_list = bool(target.get("permissions"))
    update_user_role(user_id, role)
    log("auth_change_role", admin_id=current_user["id"], target_user_id=user_id, role=role,
        permissions_reset=had_list)
    return {"status": "updated", "permissions_reset": had_list}


@router.patch("/api/users/{user_id}/department")
def change_department(user_id: int, body: dict, current_user: dict = Depends(require_permission("manage_users"))):
    # The Owner's alone (2026-10-31). An account's department is half of what
    # the chat reads for it and half of where it may write (the KB's
    # department write check); with manage_users alone, an Admin - its preset
    # holds the scope - moved any account between departments, its own
    # included, and so widened its own read and write axes to a department
    # the Owner never opened to it. Refused before anything is read or
    # written; your own department, like your own role, says so.
    from app.permissions import is_owner
    if not is_owner(current_user):
        if user_id == current_user["id"]:
            raise HTTPException(status_code=403, detail="Cannot change your own department")
        raise HTTPException(status_code=403,
                            detail="Only an Owner can change an account's department")
    dept = body.get("department", "general").strip() or "general"
    from app.help_docs import refuse_reserved_department
    refuse_reserved_department(dept)   # same reason as add_user: the other door
    update_user_department(user_id, dept)
    log("auth_change_dept", admin_id=current_user["id"], target_user_id=user_id, department=dept)
    return {"status": "updated"}


@router.patch("/api/users/{user_id}/permissions")
def change_permissions(user_id: int, body: dict, current_user: dict = Depends(require_permission("manage_users"))):
    from app.permissions import can_grant, raises_authority, resulting_permissions
    target = get_user_by_id(user_id)
    perms = body.get("permissions")
    # Step-up (2026-09-09) on the writes that RAISE authority: adding
    # manage_users or manage_system the target does not hold today. A
    # seven-scope override is a durable near-apex account whose role column
    # never changed, so the role gate never fires. Narrowing or
    # preserving writes cost nothing extra.
    if raises_authority(target, perms):
        require_step_up(current_user, body.get("current_password") or "",
                        "grant manage_users or manage_system")
    # Authority ceiling BEFORE any write - manage_users alone must not be a
    # route to manage_system (and from there to the provider keys in config).
    # Reset-to-defaults is checked too: it is still a write to the target's
    # authority, and an Owner's row stays Owner-managed.
    refusal = can_grant(current_user, target, perms or [])
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    # The no-usable-password rule - see change_role.
    refusal = password_required_for_authority(target, resulting_permissions(target, perms))
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)
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


class MfaResetRequest(BaseModel):
    # The caller's OWN password - the step-up (ruled 2026-09-21). Optional at
    # the schema so a missing value is the route's 400, never a 422.
    current_password: str = ""


@router.post("/api/admin/users/{user_id}/mfa-reset")
def admin_mfa_reset(user_id: int, body: MfaResetRequest | None = Body(None),
                    current_user: dict = Depends(require_permission("manage_users"))):
    """Admin: disable MFA for a user (e.g. lost authenticator).

    STEP-UP FIRST (ruled 2026-09-21). Stripping a second factor is a write to
    an authentication boundary, and until now a manage-users bearer alone did
    it - a stolen session on an unlocked device could switch MFA off for any
    account it could reach, its own included. The caller's password is
    required, refused with a string 400, counted against their lockout.
    Self-target is ALLOWED with the step-up and logged as such: this surface
    has no self-service disable route, so refusing it would strand a sole
    Owner who wants their own second factor off; with the password proven it
    is exactly the door a self-service route would be.

    Owner targets are Owner-only. Stripping a principal's second factor is a
    write to THEIR authentication boundary, not to your own - an Admin who can
    do it to the Owner has turned an Owner takeover from "needs the password
    and the device" into "needs the password". change_role and can_grant both
    already refuse to let an Admin act on an Owner; this is the same rule on
    the authentication axis, which was the one path still missing it.
    """
    require_step_up(current_user, body.current_password if body else "",
                    "reset a second factor")
    from app.permissions import is_owner
    target = get_user_by_id(user_id)
    if target and target.get("role") == "owner" and not is_owner(current_user):
        raise HTTPException(status_code=403,
                            detail="Only an Owner can reset an Owner's MFA")
    disable_mfa(user_id)
    # The seed the code count and any pending setup belonged to is gone, so
    # they go too (2026-10-02) - or the person enrolling again would meet a
    # cap their old seed filled. This door removes the seed the count was
    # against; an unlock leaves the seed where it is, so it never clears the
    # count.
    from app.security import clear_totp_attempts, clear_mfa_pending
    clear_totp_attempts(user_id)
    clear_mfa_pending(user_id)
    log("admin_mfa_reset", admin_id=current_user["id"], target_user_id=user_id,
        self_target=(user_id == current_user["id"]))
    return {"status": "MFA disabled"}


class UnlockRequest(BaseModel):
    # The proof, and which one is read depends on whose account it is: the
    # caller's OWN password to unlock someone else's account, a current
    # authenticator code to unlock the caller's own. Both optional at the
    # schema, like the field at every other step-up door, so a missing one is
    # the route's string 400, never a 422.
    current_password: str = ""
    mfa_code: str = ""


@router.post("/api/admin/users/{user_id}/unlock")
def admin_unlock_user(user_id: int, body: UnlockRequest | None = Body(None),
                      current_user: dict = Depends(require_permission("manage_users"))):
    """Admin: unlock a locked account.

    What it clears is the sign-in's lock and its count of misses, so the
    person can sign in again at once. The lock is what bounds password
    guesses at the sign-in for one account, so clearing it takes proof of
    the person, not a session alone - a session alone could otherwise hand
    back the guesses a lock had stopped, for any account it may unlock.

    PROOF FIRST, before the ceiling below, as at every other step-up door:

    - Someone else's account: the caller's OWN password, through the one
      step-up helper - a missing or wrong one is a string 400, a wrong one
      counts against the caller's own lock and in the count of passwords
      checked. A caller whose own account is locked is refused here in
      words that say whose lock it is; the helper would refuse it anyway.
    - The caller's own account: a current authenticator code
      (jwt_auth.require_code_to_unlock_yourself), counted like every other
      code. Not the password: the lock on this account is the count of wrong
      passwords for it, and a locked account's password is not accepted
      anywhere. Not a refusal either: an Owner who is the only Owner, and
      whose sign-in someone else keeps locking, has no other account that
      may unlock theirs. An account with no authenticator enrolled waits
      for the lock to end or asks another administrator of its tier, and
      the refusal says so, with the minutes. ONCE PER LOCKOUT WINDOW
      (security.count_own_unlock): a right code clears the lock and its
      count of misses, so an own-account unlock that could be repeated
      would hand the sign-in's guesses back without end to whoever holds a
      session and the authenticator and not the password. The second one in
      a window answers 429, a right code included.

    An administrator's unlock of ANOTHER account is deliberately not
    limited that way: it takes the administrator's own password every time,
    and the party it would be limiting is the administrator.

    Neither proof clears the two counts that outlast an unlock: the
    passwords checked at the step-up doors (security.count_password_attempt)
    end with their window or with the account's own right password there,
    and an administrator's password is not that. The authenticator codes
    (security.count_totp_attempt) end with their window or a right code -
    which the unlock of your own account is.

    Owner targets are Owner-only - the ceiling the MFA reset above carries,
    for its reason. The lock is part of the Owner's authentication boundary:
    an Admin who can clear it can hand back the guesses it stopped, as often
    as the lock lands. An Owner whose account is locked waits for the lock to
    end, unlocks it from a session of their own with a current code, or asks
    another Owner.
    """
    from app.permissions import is_owner
    if user_id == current_user["id"]:
        require_code_to_unlock_yourself(
            current_user, body.mfa_code if body else "",
            "another Owner" if is_owner(current_user) else "another administrator")
    else:
        minutes = lock_minutes_left(current_user)
        if minutes:
            own_way = (" You can unlock your own account first, with a current "
                       "authenticator code." if current_user.get("mfa_enabled") else "")
            raise HTTPException(
                status_code=429,
                detail=f"Your own account is locked, so your password is not accepted "
                       f"to unlock another account. Try again in {minutes} "
                       f"minute(s).{own_way}")
        require_step_up(current_user, body.current_password if body else "",
                        "unlock an account")
    # The target is read AFTER the proof, so a caller who fails the proof
    # learns nothing about which ids exist - and through the any-status
    # reader, because the write below is by id alone: a deactivated row is
    # still a row whose lock this would clear, so the ceiling applies to it
    # as to an active one, and an id that names no account is a 404, not an
    # "unlocked".
    target = get_user_by_id_any_status(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such user")
    if target.get("role") == "owner" and not is_owner(current_user):
        raise HTTPException(status_code=403,
                            detail="Only an Owner can unlock an Owner's account")
    unlock_user(user_id)
    log("admin_unlock_user", admin_id=current_user["id"], target_user_id=user_id,
        self_target=(user_id == current_user["id"]))
    return {"status": "unlocked"}


@router.get("/api/admin/permissions")
def admin_get_permissions(current_user: dict = Depends(require_permission("manage_users"))):
    return {"scopes": PERMISSION_SCOPES, "presets": ROLE_PERMISSIONS}
