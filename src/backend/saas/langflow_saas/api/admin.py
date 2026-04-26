"""Operator / admin endpoints for the SaaS plugin.

All routes require a Langflow superuser token (is_superuser=True).
They are mounted under /api/saas/v1/admin/ and are never exposed to
regular users.

Routes:
  GET  /admin/stats                         — platform-level metrics
  GET  /admin/users                         — list all users with org info
  GET  /admin/orgs                          — list all orgs with plan info
  PATCH /admin/orgs/{org_id}/plan           — override org plan
  PATCH /admin/orgs/{org_id}/status         — activate / deactivate org
  GET  /admin/plans                         — list all plans (including inactive)
  POST /admin/plans                         — create plan
  PATCH /admin/plans/{plan_id}              — update plan
  DELETE /admin/plans/{plan_id}             — soft-deactivate plan
  POST /admin/users/{user_id}/impersonate   — get short-lived JWT for any user
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlmodel import SQLModel, select

from langflow_saas.dependencies import RequireSuperuser
from langflow_saas.models import (
    Organization,
    Plan,
    PlanRead,
    Subscription,
    SubscriptionStatus,
    UserOrganization,
)

router = APIRouter(prefix="/admin", tags=["Admin"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PlanCreate(SQLModel):
    name: str
    slug: str
    max_flows: int = 50
    max_executions_per_day: int = 1000
    max_members: int = 5
    max_storage_mb: int = 500
    max_api_keys: int = 5
    rpm_limit: int = 60
    price_monthly_cents: int = 0
    price_yearly_cents: int = 0
    stripe_monthly_price_id: str | None = None
    stripe_yearly_price_id: str | None = None


class PlanUpdate(SQLModel):
    name: str | None = None
    max_flows: int | None = None
    max_executions_per_day: int | None = None
    max_members: int | None = None
    max_storage_mb: int | None = None
    max_api_keys: int | None = None
    rpm_limit: int | None = None
    price_monthly_cents: int | None = None
    price_yearly_cents: int | None = None
    stripe_monthly_price_id: str | None = None
    stripe_yearly_price_id: str | None = None
    is_active: bool | None = None


class OrgPlanOverride(SQLModel):
    plan_id: UUID


class OrgStatusUpdate(SQLModel):
    is_active: bool


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats")
async def platform_stats(_su: RequireSuperuser) -> dict[str, Any]:
    """Return aggregate platform metrics for the operator dashboard."""
    from langflow.services.database.models.flow.model import Flow
    from langflow.services.database.models.user.model import User
    from langflow.services.deps import session_scope
    from sqlalchemy import func

    from langflow_saas.models import AuditLog, UsageRecord

    async with session_scope() as db:
        total_users = (await db.exec(select(func.count(User.id)))).first() or 0
        total_orgs = (await db.exec(select(func.count(Organization.id)))).first() or 0
        personal_orgs = (
            await db.exec(select(func.count(Organization.id)).where(Organization.is_personal == True))  # noqa: E712
        ).first() or 0
        active_subs = (
            await db.exec(
                select(func.count(Subscription.id)).where(
                    Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING])
                )
            )
        ).first() or 0
        total_flows = (await db.exec(select(func.count(Flow.id)))).first() or 0
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        executions_today = (
            await db.exec(
                select(func.sum(UsageRecord.value)).where(UsageRecord.recorded_at >= today_start)
            )
        ).first() or 0
        audit_events_today = (
            await db.exec(
                select(func.count(AuditLog.id)).where(AuditLog.created_at >= today_start)
            )
        ).first() or 0

    return {
        "total_users": total_users,
        "total_orgs": total_orgs,
        "personal_orgs": personal_orgs,
        "team_orgs": total_orgs - personal_orgs,
        "active_subscriptions": active_subs,
        "total_flows": total_flows,
        "executions_today": executions_today,
        "audit_events_today": audit_events_today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users")
async def list_all_users(
    _su: RequireSuperuser,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List Langflow users with their org memberships."""
    from langflow.services.database.models.user.model import User
    from langflow.services.deps import session_scope

    async with session_scope() as db:
        users_result = await db.exec(
            select(User).order_by(User.username).offset(offset).limit(min(limit, 200))
        )
        users = users_result.all()

        rows = []
        for user in users:
            mem_result = await db.exec(
                select(UserOrganization, Organization)
                .join(Organization, Organization.id == UserOrganization.org_id)
                .where(UserOrganization.user_id == user.id)
            )
            memberships = mem_result.all()
            rows.append(
                {
                    "id": str(user.id),
                    "username": user.username,
                    "email": getattr(user, "email", None),
                    "is_active": user.is_active,
                    "is_superuser": user.is_superuser,
                    "orgs": [
                        {
                            "org_id": str(uo.org_id),
                            "org_name": org.name,
                            "org_slug": org.slug,
                            "is_personal": org.is_personal,
                            "role": uo.role.value,
                        }
                        for uo, org in memberships
                    ],
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Orgs
# ---------------------------------------------------------------------------


@router.get("/orgs")
async def list_all_orgs(
    _su: RequireSuperuser,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List all orgs with their current plan and subscription status."""
    from langflow.services.deps import session_scope

    async with session_scope() as db:
        orgs_result = await db.exec(
            select(Organization).order_by(Organization.created_at.desc()).offset(offset).limit(min(limit, 200))
        )
        orgs = orgs_result.all()

        rows = []
        for org in orgs:
            plan = None
            if org.plan_id:
                plan_r = await db.exec(select(Plan).where(Plan.id == org.plan_id))
                plan = plan_r.first()

            sub_r = await db.exec(select(Subscription).where(Subscription.org_id == org.id))
            sub = sub_r.first()

            member_count = len(
                (await db.exec(select(UserOrganization).where(UserOrganization.org_id == org.id))).all()
            )

            rows.append(
                {
                    "id": str(org.id),
                    "name": org.name,
                    "slug": org.slug,
                    "is_personal": org.is_personal,
                    "is_active": org.is_active,
                    "member_count": member_count,
                    "plan": {"id": str(plan.id), "name": plan.name, "slug": plan.slug} if plan else None,
                    "subscription_status": sub.status.value if sub else None,
                    "stripe_customer_id": org.stripe_customer_id,
                    "created_at": org.created_at.isoformat(),
                }
            )
    return rows


@router.patch("/orgs/{org_id}/plan", status_code=status.HTTP_200_OK)
async def override_org_plan(org_id: UUID, body: OrgPlanOverride, _su: RequireSuperuser):
    """Override the plan assigned to an org (no billing change — operator only)."""
    from langflow.services.deps import session_scope

    async with session_scope() as db:
        org_r = await db.exec(select(Organization).where(Organization.id == org_id))
        org = org_r.first()
        if not org:
            raise HTTPException(404, "Organization not found.")

        plan_r = await db.exec(select(Plan).where(Plan.id == body.plan_id, Plan.is_active == True))  # noqa: E712
        plan = plan_r.first()
        if not plan:
            raise HTTPException(404, "Plan not found or inactive.")

        org.plan_id = body.plan_id
        org.updated_at = datetime.now(timezone.utc)
        db.add(org)

        # Update or create the subscription row to reflect the new plan.
        sub_r = await db.exec(select(Subscription).where(Subscription.org_id == org_id))
        sub = sub_r.first()
        now = datetime.now(timezone.utc)
        if sub:
            sub.plan_id = body.plan_id
            sub.updated_at = now
            db.add(sub)
        else:
            db.add(Subscription(org_id=org_id, plan_id=body.plan_id, status=SubscriptionStatus.ACTIVE,
                                created_at=now, updated_at=now))

        await db.commit()

    return {"ok": True, "org_id": str(org_id), "plan_id": str(body.plan_id)}


@router.patch("/orgs/{org_id}/status", status_code=status.HTTP_200_OK)
async def set_org_status(org_id: UUID, body: OrgStatusUpdate, _su: RequireSuperuser):
    """Activate or deactivate an org (deactivated orgs get 403 on all SaaS routes)."""
    from langflow.services.deps import session_scope

    async with session_scope() as db:
        org_r = await db.exec(select(Organization).where(Organization.id == org_id))
        org = org_r.first()
        if not org:
            raise HTTPException(404, "Organization not found.")
        org.is_active = body.is_active
        org.updated_at = datetime.now(timezone.utc)
        db.add(org)
        await db.commit()

    return {"ok": True, "org_id": str(org_id), "is_active": body.is_active}


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@router.get("/plans", response_model=list[PlanRead])
async def list_all_plans(_su: RequireSuperuser):
    """List all plans including inactive ones."""
    from langflow.services.deps import session_scope

    async with session_scope() as db:
        result = await db.exec(select(Plan).order_by(Plan.price_monthly_cents))
        return [PlanRead.model_validate(p) for p in result.all()]


@router.post("/plans", response_model=PlanRead, status_code=status.HTTP_201_CREATED)
async def create_plan(body: PlanCreate, _su: RequireSuperuser):
    from langflow.services.deps import session_scope

    async with session_scope() as db:
        # Check slug uniqueness.
        existing = await db.exec(select(Plan).where(Plan.slug == body.slug))
        if existing.first():
            raise HTTPException(409, f"A plan with slug '{body.slug}' already exists.")

        now = datetime.now(timezone.utc)
        plan = Plan(**body.model_dump(), is_active=True, created_at=now, updated_at=now)
        db.add(plan)
        await db.commit()
        await db.refresh(plan)

    return PlanRead.model_validate(plan)


@router.patch("/plans/{plan_id}", response_model=PlanRead)
async def update_plan(plan_id: UUID, body: PlanUpdate, _su: RequireSuperuser):
    from langflow.services.deps import session_scope

    async with session_scope() as db:
        result = await db.exec(select(Plan).where(Plan.id == plan_id))
        plan = result.first()
        if not plan:
            raise HTTPException(404, "Plan not found.")

        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(plan, field, value)
        plan.updated_at = datetime.now(timezone.utc)
        db.add(plan)
        await db.commit()
        await db.refresh(plan)

    return PlanRead.model_validate(plan)


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_plan(plan_id: UUID, _su: RequireSuperuser):
    """Soft-deactivate a plan (sets is_active=False; existing subs are unaffected)."""
    from langflow.services.deps import session_scope

    async with session_scope() as db:
        result = await db.exec(select(Plan).where(Plan.id == plan_id))
        plan = result.first()
        if not plan:
            raise HTTPException(404, "Plan not found.")
        plan.is_active = False
        plan.updated_at = datetime.now(timezone.utc)
        db.add(plan)
        await db.commit()


# ---------------------------------------------------------------------------
# Impersonation
# ---------------------------------------------------------------------------


@router.post("/users/{user_id}/impersonate")
async def impersonate_user(user_id: UUID, _su: RequireSuperuser) -> dict[str, str]:
    """Issue a short-lived JWT (15 min) for any user — for operator support use only."""
    from langflow.services.deps import get_settings_service, session_scope
    from sqlmodel import select

    from langflow.services.database.models.user.model import User

    async with session_scope() as db:
        result = await db.exec(select(User).where(User.id == user_id))
        target = result.first()
        if not target:
            raise HTTPException(404, "User not found.")

    try:
        import jwt as pyjwt

        settings_service = get_settings_service()
        secret = settings_service.auth_settings.SECRET_KEY.get_secret_value()
        algo = settings_service.auth_settings.ALGORITHM.value

        payload = {
            "sub": str(target.id),
            "username": target.username,
            "impersonated_by": "superuser",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        }
        token = pyjwt.encode(payload, secret, algorithm=algo)
    except Exception as exc:
        raise HTTPException(500, "Failed to generate impersonation token.") from exc

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": "15 minutes",
        "user_id": str(target.id),
        "username": target.username,
    }
