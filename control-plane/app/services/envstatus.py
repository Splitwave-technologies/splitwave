"""Состояние среды для карточки проекта и списка: готова / собирается / сбой / нет данных.

Проекты с buildpacks спрашиваем у kpack (там есть объект Image). Проекты с Dockerfile (kaniko) и среды на серверах объекта kpack не имеют:
для них состояние и образ берутся из последнего релиза (раньше такие проекты вечно показывали «Сборка…» и пустой образ)."""
from app.db.models import Environment, Release
from app.services import kpack

BUSY = ("pending", "building")


def _from_releases(db, env: Environment) -> dict:
    rels = db.query(Release).filter_by(environment_id=env.id).order_by(Release.created_at.desc()).limit(20).all()
    if not rels:
        return {"ready": None, "latest_image": None, "state": "unknown", "error": None}
    latest = rels[0]
    image = next((r.image_digest for r in rels if r.status == "deployed" and r.image_digest), None)
    if latest.status in BUSY:
        return {"ready": False, "latest_image": image, "state": "building", "error": None}
    if latest.status == "failed":
        return {"ready": None, "latest_image": image, "state": "failed", "error": (latest.error_message or "")[:200] or None}
    return {"ready": True, "latest_image": image, "state": "ready", "error": None}


def environment_status(db, env: Environment) -> dict:
    """{ready, latest_image, state, error}. Исключения кластера (для kpack) пробрасываются вызывающему."""
    if env.project.build_method == "dockerfile" or env.server:
        return _from_releases(db, env)
    st = kpack.get_status(env.image_name, env.build_namespace)
    state = "ready" if st["ready"] else "building"
    return {"ready": st["ready"], "latest_image": st["latest_image"], "state": state, "error": None}
