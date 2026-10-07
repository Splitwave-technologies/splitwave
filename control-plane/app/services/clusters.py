"""Выбор кластера для среды. Удалённые кластеры — платная функция multi_cluster (модуль регистрирует поставщика клиентов)."""
from typing import Optional

from fastapi import HTTPException

from app import plugins
from app.licensing import current_license

LOCAL = "local"


def normalize(name: Optional[str]) -> Optional[str]:
    """None/""/"local" -> None (кластер платформы). Иначе имя должно быть зарегистрировано и разрешено лицензией."""
    if not name or name == LOCAL:
        return None
    if "multi_cluster" not in current_license().features:
        raise HTTPException(status_code=403, detail={"error": "feature_not_licensed", "feature": "multi_cluster"})
    if name not in plugins.cluster_names():
        raise HTTPException(status_code=422, detail=f"unknown cluster {name}")
    return name
