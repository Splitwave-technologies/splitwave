from fastapi import APIRouter, Depends

from app import licensing
from app.auth import Principal, require
from app.config import settings
from app.db.base import SessionLocal
from app.licensing import current_license
from app.services import instance as instance_svc
from app.services import limits as limits_svc

router = APIRouter(prefix="/api/license", tags=["license"])


@router.get("")
async def license_info(request_principal: Principal = Depends(require("license:read"))):
    """Статус лицензии, доступные платные функции, лимиты редакции и текущее использование. Сам ключ не возвращается."""
    from app.main import app  # ленивый импорт, чтобы избежать цикла
    db = SessionLocal()
    try:
        usage = limits_svc.usage(db)
    finally:
        db.close()
    return {**current_license().public(), "install_id": instance_svc.get_id(), "ee_loaded": bool(getattr(app.state, "ee_loaded", False)),
            "limits": licensing.limits(), "usage": usage, "limits_enforced": settings.tier_limits_enforce}
