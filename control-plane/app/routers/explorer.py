"""Исследователь событий: поиск по аудиту запросом вида `action:deploy* AND project:shop -actor:ci-bot`, диапазон времени,
гистограмма по времени и частые значения полей (для боковой панели). Только чтение; право audit:read."""
import shlex
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, cast, func, not_, or_, String

from app.auth import Principal, require
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Project

router = APIRouter(prefix="/api/explorer", tags=["explorer"])

FIELDS = ("action", "actor", "project", "result")
RANGES = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}
SCAN_CAP = 20000                                  # сколько последних событий просматривается для гистограммы и боковой панели
FAILED_MARKS = ("failed", "denied", "error", "rejected", "forbidden")


class QueryError(ValueError):
    pass


def parse_query(q: str) -> list[dict]:
    """`field:value` (значение может содержать * и пробелы в кавычках), `-field:value` — исключить, слово без поля — поиск по тексту.
    Слова AND игнорируются (все условия объединяются через И); OR не поддерживается."""
    try:
        tokens = shlex.split(q or "")
    except ValueError:
        raise QueryError("unbalanced quotes in the query")
    conds = []
    for tok in tokens:
        if tok.upper() == "AND":
            continue
        if tok.upper() == "OR":
            raise QueryError("OR is not supported: use several searches")
        neg = tok.startswith("-") and len(tok) > 1
        body = tok[1:] if neg else tok
        if ":" in body:
            field, value = body.split(":", 1)
            field = field.lower()
            if field not in FIELDS:
                raise QueryError(f"unknown field '{field}'; use one of: {', '.join(FIELDS)}")
            if not value:
                raise QueryError(f"empty value for '{field}'")
            conds.append({"field": field, "value": value, "neg": neg})
        else:
            conds.append({"field": "text", "value": body, "neg": neg})
    if len(conds) > 12:
        raise QueryError("too many conditions (max 12)")
    return conds


def _like(col, value: str):
    esc = value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_").replace("*", "%")
    return func.lower(col).like(esc.lower() if "*" in value else esc.lower(), escape="\\") if "*" in value else func.lower(col) == value.lower()


def _failed_expr():
    return or_(*[func.lower(AuditEvent.action).like(f"%{m}%") for m in FAILED_MARKS])


def build_filter(db, conds: list[dict]):
    clauses = []
    for c in conds:
        f, v = c["field"], c["value"]
        if f == "action":
            e = _like(AuditEvent.action, v)
        elif f == "actor":
            e = _like(AuditEvent.actor, v)
        elif f == "project":
            ids = [p.id for p in db.query(Project).all() if _match_slug(p.slug, v)]
            e = AuditEvent.project_id.in_(ids) if ids else AuditEvent.project_id.is_(None) & AuditEvent.id.is_(None)
        elif f == "result":
            if v.lower() not in ("ok", "failed"):
                raise QueryError("result must be ok or failed")
            e = _failed_expr() if v.lower() == "failed" else not_(_failed_expr())
        else:
            like = f"%{v.lower().replace('%', '').replace('_', '')}%"
            e = or_(func.lower(AuditEvent.action).like(like), func.lower(AuditEvent.actor).like(like), func.lower(cast(AuditEvent.detail, String)).like(like))
        clauses.append(not_(e) if c["neg"] else e)
    return and_(*clauses) if clauses else None


def _match_slug(slug: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(slug.lower(), pattern.lower()) if "*" in pattern else slug.lower() == pattern.lower()


def _bucket(span: timedelta) -> int:
    for sec in (60, 300, 900, 3600, 10800, 21600, 86400):
        if span.total_seconds() / sec <= 60:
            return sec
    return 86400


def _result(action: str) -> str:
    a = (action or "").lower()
    return "failed" if any(m in a for m in FAILED_MARKS) else "ok"


@router.get("/events")
def events(q: str = Query(default="", max_length=500), range_: Optional[str] = Query(default="24h", alias="range"), start: Optional[datetime] = None, end: Optional[datetime] = None,
           limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0, le=100000), principal: Principal = Depends(require("audit:read"))):
    now = datetime.now(timezone.utc)
    if start or end:
        t1, t0 = (end or now), (start or (end or now) - timedelta(days=1))
        if t1.tzinfo is None:
            t1 = t1.replace(tzinfo=timezone.utc)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        if t0 >= t1 or (t1 - t0) > timedelta(days=366):
            raise HTTPException(status_code=422, detail="range must be positive and not longer than 366 days")
    elif range_ in RANGES:
        t1, t0 = now, now - RANGES[range_]
    else:
        raise HTTPException(status_code=422, detail=f"range must be one of {', '.join(RANGES)} or start/end")
    try:
        conds = parse_query(q)
    except QueryError as e:
        raise HTTPException(status_code=422, detail=str(e))
    db = SessionLocal()
    try:
        try:
            flt = build_filter(db, conds)
        except QueryError as e:
            raise HTTPException(status_code=422, detail=str(e))
        base = db.query(AuditEvent).filter(AuditEvent.created_at >= t0, AuditEvent.created_at <= t1)
        if flt is not None:
            base = base.filter(flt)
        total = base.count()
        page = base.order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit).all()
        scanned = base.order_by(AuditEvent.created_at.desc()).limit(SCAN_CAP).with_entities(AuditEvent.created_at, AuditEvent.action, AuditEvent.actor, AuditEvent.project_id).all()
        slugs = {p.id: p.slug for p in db.query(Project).all()}
        sec = _bucket(t1 - t0)
        first = int(t0.timestamp() // sec) * sec
        n_buckets = int((t1.timestamp() - first) // sec) + 1
        hist = [{"t": datetime.fromtimestamp(first + i * sec, timezone.utc).isoformat(), "n": 0, "failed": 0} for i in range(n_buckets)]
        counts = {f: {} for f in FIELDS}
        for ts, action, actor, pid in scanned:
            tsu = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            i = int((tsu.timestamp() - first) // sec)
            if 0 <= i < n_buckets:
                hist[i]["n"] += 1
                if _result(action) == "failed":
                    hist[i]["failed"] += 1
            for f, v in (("action", action), ("actor", actor), ("result", _result(action)), ("project", slugs.get(pid))):
                if v:
                    counts[f][v] = counts[f].get(v, 0) + 1
        fields = {f: [{"value": v, "count": n} for v, n in sorted(c.items(), key=lambda x: -x[1])[:8]] for f, c in counts.items()}
        return {"total": total, "scanned": len(scanned), "capped": total > SCAN_CAP, "range": {"start": t0.isoformat(), "end": t1.isoformat()},
                "query": [{"field": c["field"], "value": c["value"], "neg": c["neg"]} for c in conds],
                "histogram": {"bucket_seconds": sec, "buckets": hist}, "fields": fields,
                "events": [{"id": str(e.id), "seq": e.seq, "time": e.created_at.isoformat() if e.created_at else None, "actor": e.actor, "action": e.action,
                            "result": _result(e.action), "project": slugs.get(e.project_id), "detail": e.detail, "context": e.context or {}} for e in page]}
    finally:
        db.close()
