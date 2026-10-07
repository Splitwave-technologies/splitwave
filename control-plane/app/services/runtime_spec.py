"""Параметры контейнера для сред на серверах. Проверяются строго: по ним формируется команда docker на чужом сервере."""
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]*$")


class PortMap(BaseModel):
    host: int = Field(ge=1, le=65535)
    container: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"
    bind: Literal["0.0.0.0", "127.0.0.1"] = "0.0.0.0"     # 127.0.0.1 — порт виден только на самом сервере (за обратным прокси)


class VolumeMount(BaseModel):
    name: str
    path: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("volume name: lowercase letters, digits, _ . -")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not PATH_RE.match(v) or ".." in v.split("/") or v == "/":
            raise ValueError("volume path must be an absolute path without '..'")
        return v


class HealthCheck(BaseModel):
    mode: Literal["http", "running"] = "running"      # http: GET по адресу на самом сервере; running: контейнер живёт заданное время
    port: Optional[int] = Field(default=None, ge=1, le=65535)    # опубликованный порт сервера
    path: str = "/"
    timeout_seconds: int = Field(default=60, ge=5, le=900)

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/") or not re.match(r"^/[A-Za-z0-9_./?=&%-]*$", v):
            raise ValueError("health path must start with '/'")
        return v


class RuntimeSpec(BaseModel):
    ports: list[PortMap] = Field(default_factory=list, max_length=20)
    volumes: list[VolumeMount] = Field(default_factory=list, max_length=10)
    restart: Literal["no", "on-failure", "unless-stopped", "always"] = "unless-stopped"
    memory: Optional[str] = Field(default=None, pattern=r"^[1-9][0-9]{1,5}[mMgG]$")
    cpus: Optional[float] = Field(default=None, ge=0.1, le=64)
    health: HealthCheck = Field(default_factory=HealthCheck)

    @field_validator("health")
    @classmethod
    def _health(cls, v: HealthCheck, info) -> HealthCheck:
        if v.mode == "http":
            ports = {p.host for p in (info.data.get("ports") or [])}
            if v.port is None or v.port not in ports:
                raise ValueError("http health check needs a port that is published in ports[].host")
        return v


def validate(spec: Optional[dict]) -> dict:
    """Возвращает нормализованные параметры; ValueError — неверные данные."""
    from pydantic import ValidationError
    try:
        return RuntimeSpec(**(spec or {})).model_dump()
    except ValidationError as e:
        raise ValueError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:400])
