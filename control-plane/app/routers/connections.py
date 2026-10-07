"""Проверка подключения к внешним сервисам проекта (БД, кэш, S3, HTTPS на отдельных хостах)."""
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import Principal, require
from app.config import settings
from app.db.base import SessionLocal
from app.services import audit, connections, project_repo

router = APIRouter(prefix="/api/projects/{slug}", tags=["connections"])

MAX_TARGETS = 10
_hits: dict = defaultdict(deque)
_lock = threading.Lock()


class ProbeBody(BaseModel):
    targets: list[dict]


def _rate_limit(who: str, count: int) -> None:
    """Не более CONNECTION_CHECK_PER_MINUTE проверок в минуту на пользователя: платформа не должна служить сканером портов."""
    now = time.monotonic()
    with _lock:
        q = _hits[who]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) + count > settings.connection_check_per_minute:
            raise HTTPException(status_code=429, detail="too many connection checks, try again in a minute")
        q.extend([now] * count)


@router.post("/connections/probe")
def probe(slug: str, body: ProbeBody, principal: Principal = Depends(require("deploy"))):
    """Разовая проверка: DNS → TCP → TLS (разбор сертификата) → вход. Учётные данные задаются в запросе, нигде не сохраняются
    и не попадают в ответ и журнал (в журнал — только тип, адрес, режим TLS и итог)."""
    if not 1 <= len(body.targets) <= MAX_TARGETS:
        raise HTTPException(status_code=422, detail=f"targets: 1..{MAX_TARGETS} items")
    db = SessionLocal()
    try:
        project = project_repo.get_project_by_slug(db, slug)
        if not project:
            raise HTTPException(status_code=404, detail=f"unknown project {slug}")
        try:
            specs = [connections.validate_spec(t) for t in body.targets]
        except connections.SpecError as e:
            raise HTTPException(status_code=422, detail=str(e))
        _rate_limit(principal.name, len(specs))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda s: connections.check(s), specs))
        audit.log_event(db, principal.name, "connection_check", project.id, detail={"targets": [
            {"kind": r["target"]["kind"], "host": r["target"]["host"], "port": r["target"]["port"], "tls": r["target"]["tls"],
             "ok": r["ok"], "code": r["code"]} for r in results]})
        return {"results": results}
    finally:
        db.close()
