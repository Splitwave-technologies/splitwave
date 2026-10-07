"""Файлы агента для установки на серверы: сам агент (открытый код), его контрольная сумма и скрипт установки. Без авторизации и без секретов."""
import hashlib
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app.netutil import is_https

router = APIRouter(prefix="/agent", tags=["agent-files"], include_in_schema=False)
DIR = Path(__file__).resolve().parents[1] / "agent"


def _agent() -> bytes:
    return (DIR / "dsp_agent.py").read_bytes()


def base_url(request: Request) -> str:
    from app.config import settings
    if settings.public_url:
        return settings.public_url.rstrip("/")
    scheme = "https" if is_https(request) else "http"
    return f"{scheme}://{request.headers.get('host', request.url.netloc)}"


@router.get("/dsp_agent.py")
async def agent_source():
    return PlainTextResponse(_agent().decode(), media_type="text/x-python")


@router.get("/dsp_agent.py.sha256")
async def agent_checksum():
    return PlainTextResponse(hashlib.sha256(_agent()).hexdigest())


@router.get("/install.sh")
async def install_script(request: Request):
    text = (DIR / "install.sh.tpl").read_text().replace("__PLATFORM_URL__", base_url(request))
    return PlainTextResponse(text, media_type="text/x-shellscript")
