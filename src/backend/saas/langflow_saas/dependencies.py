"""FastAPI dependency callables for the SaaS plugin.

All SaaS route handlers use these deps instead of Langflow's
``get_current_active_user`` so that org context and RBAC checks are
applied consistently.

Dependency graph:
    CurrentOrgContext          — resolves OrgContextData from request.state
        └─ RequireOrgRole(...)     — asserts a minimum role level
            └─ RequireOrgAdmin     — shortcut for admin+
            └─ RequireOrgOwner     — shortcut for owner only
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from langflow_saas.middleware import OrgContextData
from langflow_saas.models import OrgRole

# Role ordering for gte comparison.
_ROLE_RANK: dict[OrgRole, int] = {
    OrgRole.VIEWER: 0,
    OrgRole.MEMBER: 1,
    OrgRole.ADMIN: 2,
    OrgRole.OWNER: 3,
}


async def get_org_context(request: Request) -> OrgContextData:
    """Retrieve the tenant context set by TenantContextMiddleware.

    Raises 401 if the context is absent (unauthenticated request) and 403
    if the user is authenticated but has no org membership (should not
    happen in normal flows after personal-org auto-creation is enabled).
    """
    ctx: OrgContextData | None = getattr(request.state, "saas_context", None)
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide a Bearer token, cookie, or x-api-key.",
        )
    return ctx


CurrentOrgContext = Annotated[OrgContextData, Depends(get_org_context)]


def require_role(minimum_role: OrgRole):
    """Dependency factory: asserts caller has at least ``minimum_role``."""

    async def _check(ctx: CurrentOrgContext) -> OrgContextData:
        if _ROLE_RANK[ctx.role] < _ROLE_RANK[minimum_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires {minimum_role.value} role or higher. "
                f"Your role in this org is {ctx.role.value}.",
            )
        return ctx

    return _check


RequireMember = Annotated[OrgContextData, Depends(require_role(OrgRole.MEMBER))]
RequireAdmin = Annotated[OrgContextData, Depends(require_role(OrgRole.ADMIN))]
RequireOwner = Annotated[OrgContextData, Depends(require_role(OrgRole.OWNER))]


# ---------------------------------------------------------------------------
# Superuser dependency — wraps Langflow's is_superuser flag.
# Admin API endpoints use this so only server operators can access them.
# ---------------------------------------------------------------------------


async def require_superuser(request: Request):
    """Resolve the current Langflow user and assert is_superuser=True."""
    # Re-use Langflow's own superuser dependency by manually calling it
    # with the request's Authorization header.
    token = request.headers.get("Authorization", "")
    if token.startswith("Bearer "):
        token = token[7:]
    if not token:
        token = request.cookies.get("access_token_lf", "")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    try:
        import jwt as pyjwt
        from langflow.services.auth.utils import get_jwt_verification_key
        from langflow.services.deps import get_settings_service, session_scope
        from sqlmodel import select

        settings_service = get_settings_service()
        algorithm = settings_service.auth_settings.ALGORITHM
        key = get_jwt_verification_key(settings_service)

        from uuid import UUID as UUIDType

        payload = pyjwt.decode(token, key, algorithms=[algorithm], options={"verify_exp": True})
        user_id_str = payload.get("sub")
        if not user_id_str:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
        user_id = UUIDType(user_id_str)

        from langflow.services.database.models.user.model import User

        async with session_scope() as db:
            result = await db.exec(select(User).where(User.id == user_id))
            user = result.first()

        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")
        if not user.is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Superuser access required for admin operations.",
            )
        return user
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.") from exc


RequireSuperuser = Annotated[object, Depends(require_superuser)]


# ---------------------------------------------------------------------------
# Shorthand for reading a UUID path param and validating it matches the
# authenticated org (prevents IDOR on org-scoped resources).
# ---------------------------------------------------------------------------


def assert_org_match(path_org_id: UUID, ctx: OrgContextData) -> None:
    """Raise 403 if the path org_id doesn't match the authenticated context."""
    if path_org_id != ctx.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this organization.",
        )
