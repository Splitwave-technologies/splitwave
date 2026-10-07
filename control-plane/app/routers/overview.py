"""Сводка для главной страницы интерфейса: показатели и данные для графиков за период."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from app.auth import Principal, require_scoped
from app.db.base import SessionLocal
from app.services import secrets as secrets_svc
from app.db.models import ApiToken, AuditEvent, Approval, Environment, Project, Release, Secret

router = APIRouter(prefix="/api/overview", tags=["overview"])


def _utc(dt):
    """SQLite возвращает даты без часового пояса — считаем их UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("")
async def overview(days: int = Query(default=30, ge=1, le=90), principal: Principal = Depends(require_scoped("read"))):
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

        allowed = principal.allowed_project_ids()
        projects = [p for p in db.query(Project).all() if allowed is None or str(p.id) in allowed]
        visible = {p.id for p in projects}
        env_to_project = {e.id: e.project_id for e in db.query(Environment).all() if e.project_id in visible}
        slug_of = {p.id: p.slug for p in projects}

        releases = [r for r in db.query(Release).all()
                    if r.environment_id in env_to_project and _utc(r.created_at) and _utc(r.created_at) >= since]

        per_day: dict[str, Counter] = defaultdict(Counter)
        per_project: dict[str, Counter] = defaultdict(Counter)
        last_deploy: dict[str, datetime] = {}
        build_seconds = []
        for r in releases:
            day = _utc(r.created_at).date().isoformat()
            bucket = r.status if r.status in ("deployed", "failed") else "other"
            per_day[day][bucket] += 1
            slug = slug_of.get(env_to_project.get(r.environment_id))
            if slug:
                per_project[slug][bucket] += 1
                if r.status == "deployed":
                    ts = _utc(r.deployed_at) or _utc(r.created_at)
                    if slug not in last_deploy or ts > last_deploy[slug]:
                        last_deploy[slug] = ts
            if r.status == "deployed" and r.deployed_at and r.created_at:
                delta = (_utc(r.deployed_at) - _utc(r.created_at)).total_seconds()
                if 0 <= delta < 6 * 3600:
                    build_seconds.append(delta)

        series = []
        for i in range(days):
            d = (since + timedelta(days=i)).date().isoformat()
            c = per_day.get(d, Counter())
            series.append({"date": d, "deployed": c["deployed"], "failed": c["failed"], "other": c["other"]})

        ok = sum(1 for r in releases if r.status == "deployed")
        bad = sum(1 for r in releases if r.status == "failed")

        events = (db.query(AuditEvent).filter(AuditEvent.created_at >= since).order_by(AuditEvent.created_at.desc()).all()
                  if principal.can("audit:read") else [])
        failures = Counter((e.detail or {}).get("reason", "unknown") for e in events if e.action == "deploy_failed")
        denied_24h = sum(1 for e in events if e.action == "access_denied" and _utc(e.created_at) >= now - timedelta(hours=24))

        can_audit = principal.can("audit:read")
        recent = [{"time": _utc(e.created_at).isoformat(), "actor": e.actor, "action": e.action, "detail": e.detail or {}}
                  for e in events[:12]] if can_audit else None

        alerts = []
        for sec in (db.query(Secret).filter(Secret.project_id.in_(visible)).all() if visible else []):
            st = secrets_svc.secret_status(sec, now)
            if st != "ok" and principal.can("secrets:list", sec.project_id):
                alerts.append({"project": slug_of.get(sec.project_id, ""), "key": sec.key,
                               "scope": sec.scope, "status": st,
                               "expires_at": secrets_svc._utc(sec.expires_at).isoformat() if sec.expires_at else None})
        order = {"expired": 0, "expiring": 1, "rotation_due": 2}
        alerts.sort(key=lambda a: (order[a["status"]], a["project"], a["key"]))

        return {
            "days": days,
            "totals": {
                "projects": len(projects), "environments": len(env_to_project),
                "secrets": db.query(Secret).filter(Secret.project_id.in_(visible)).count() if visible else 0,
                "tokens_active": db.query(ApiToken).filter(ApiToken.revoked_at.is_(None)).count() if principal.can("tokens:manage") else None,
                "releases": len(releases),
                "pending_approvals": (db.query(Approval).filter(Approval.status == "pending", Approval.project_id.in_(visible)).count() if visible else 0),
            },
            "success_rate": round(ok / (ok + bad), 4) if ok + bad else None,
            "avg_build_seconds": round(sum(build_seconds) / len(build_seconds)) if build_seconds else None,
            "denied_24h": denied_24h,
            "secret_alerts": alerts[:20], "secret_alerts_total": len(alerts),
            "deploys_by_day": series,
            "by_project": sorted(
                [{"slug": s, "deployed": c["deployed"], "failed": c["failed"], "other": c["other"],
                  "last_deploy_at": last_deploy[s].isoformat() if s in last_deploy else None}
                 for s, c in per_project.items()],
                key=lambda x: -(x["deployed"] + x["failed"] + x["other"]))[:10],
            "failures": [{"reason": r, "count": n} for r, n in failures.most_common(6)],
            "recent": recent,
        }
    finally:
        db.close()
