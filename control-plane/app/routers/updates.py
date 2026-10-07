"""Есть ли новая версия платформы (только администратор)."""
from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.auth import Principal, require
from app.services import updates

router = APIRouter(tags=["updates"])


@router.get("/api/updates")
async def get_updates(principal: Principal = Depends(require("tokens:manage"))):
    return await run_in_threadpool(updates.status)
