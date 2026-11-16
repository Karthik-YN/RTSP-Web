from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..schemas import LoginIn, PasswordChangeIn, UserIn, UserOut, UserUpdateIn
from ..services.auth import User, client_ip, make_token
from .deps import SESSION_COOKIE, Context, current_user, get_ctx, require_admin

router = APIRouter(prefix="/api/auth", tags=["auth"])
users_router = APIRouter(prefix="/api/users", tags=["users"])


def _out(ctx: Context, user: User) -> UserOut:
    return UserOut(username=user.username, role=user.role, can_write=user.can_write, is_admin=user.is_admin,
                   auth_enabled=ctx.settings.auth_enabled)


@router.post("/login", response_model=UserOut)
def login(body: LoginIn, request: Request, response: Response, ctx: Context = Depends(get_ctx)):
    if not ctx.settings.auth_enabled:
        return _out(ctx, User("anonymous", "admin"))
    keys = (client_ip(request), body.username)
    wait = ctx.throttle.retry_after(*keys)
    if wait > 0:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"Too many failed logins. Try again in {int(wait) + 1} s.",
                            headers={"Retry-After": str(int(wait) + 1)})
    user = ctx.users.authenticate(body.username, body.password)
    if user is None:
        ctx.throttle.failure(*keys)
        ctx.events.add("auth.failed", f"failed login for '{body.username[:64]}' from {keys[0]}", level="WARNING")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong user name or password.")
    ctx.throttle.success(*keys)
    token = make_token(ctx.secret, user.username, ctx.settings.session_hours)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict",
                        secure=ctx.settings.cookie_secure,
                        max_age=int(ctx.settings.session_hours * 3600), path="/")
    ctx.events.add("auth.login", "logged in", username=user.username)
    return _out(ctx, user)


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user), ctx: Context = Depends(get_ctx)):
    return _out(ctx, user)


@router.post("/password", status_code=204)
def change_password(body: PasswordChangeIn, user: User = Depends(current_user), ctx: Context = Depends(get_ctx)):
    if not ctx.settings.auth_enabled:
        raise HTTPException(400, "Authentication is disabled on this server.")
    if ctx.users.authenticate(user.username, body.current_password) is None:
        raise HTTPException(403, "The current password is wrong.")
    try:
        ctx.users.set_user(user.username, user.role, body.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    ctx.events.add("auth.password_changed", "changed own password", username=user.username)


# ------------------------------------------------------------------ admin: users

@users_router.get("", response_model=list[UserOut])
def list_users(ctx: Context = Depends(get_ctx), _: User = Depends(require_admin)):
    return [_out(ctx, u) for u in ctx.users.list_users()]


@users_router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserIn, ctx: Context = Depends(get_ctx), admin: User = Depends(require_admin)):
    if ctx.users.get(body.username):
        raise HTTPException(409, f"User '{body.username}' already exists.")
    try:
        ctx.users.set_user(body.username, body.role, body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    ctx.events.add("users.created", f"created user '{body.username}' ({body.role})", username=admin.username)
    return _out(ctx, ctx.users.get(body.username))


@users_router.patch("/{username}", response_model=UserOut)
def update_user(username: str, body: UserUpdateIn, ctx: Context = Depends(get_ctx),
                admin: User = Depends(require_admin)):
    current = ctx.users.get(username)
    if current is None:
        raise HTTPException(404, f"User '{username}' not found.")
    role = body.role or current.role
    if current.is_admin and role != "admin" and _admin_count(ctx) <= 1:
        raise HTTPException(409, "This is the last administrator; make someone else admin first.")
    try:
        ctx.users.set_user(username, role, body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    what = ", ".join(x for x in (f"role {role}" if body.role else "", "password reset" if body.password else "") if x)
    ctx.events.add("users.updated", f"updated '{username}': {what or 'no change'}", username=admin.username)
    return _out(ctx, ctx.users.get(username))


@users_router.delete("/{username}", status_code=204)
def delete_user(username: str, ctx: Context = Depends(get_ctx), admin: User = Depends(require_admin)):
    current = ctx.users.get(username)
    if current is None:
        raise HTTPException(404, f"User '{username}' not found.")
    if username == admin.username:
        raise HTTPException(409, "You cannot delete your own account.")
    if current.is_admin and _admin_count(ctx) <= 1:
        raise HTTPException(409, "This is the last administrator.")
    ctx.users.delete_user(username)
    ctx.events.add("users.deleted", f"deleted user '{username}'", username=admin.username)


def _admin_count(ctx: Context) -> int:
    return sum(1 for u in ctx.users.list_users() if u.is_admin)
