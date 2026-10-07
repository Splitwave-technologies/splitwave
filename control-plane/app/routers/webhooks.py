import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.config import settings
from app.db.base import SessionLocal
from app.services import audit, crypto, deploy, kaniko, kpack, project_repo, scm, targets

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhooks"])


def _verify_signature(payload: bytes, signature_header: Optional[str]) -> None:
    if not settings.github_webhook_secret:
        raise HTTPException(status_code=500, detail="github_webhook_secret not configured")
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="missing signature")
    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="invalid signature")


def _fail_release(db, release, project, environment, actor: str, reason: str, message: str, revision: str) -> None:
    release.status = "failed"
    release.error_message = message[:2000]
    db.commit()
    audit.log_event(db, actor, "deploy_failed", project.id, environment.id, {"reason": reason, "revision": revision})


def run_build_and_deploy(project_id, environment_id, git_url: str, revision: str, triggered_by: str) -> None:
    from app.db.models import Environment, Project, Release
    from app.services import secrets as secrets_svc

    db = SessionLocal()
    release = project = environment = None
    stage = "init"
    try:
        project = db.get(Project, project_id)
        environment = db.get(Environment, environment_id)

        release = Release(
            environment_id=environment.id,
            git_revision=revision,
            status="building",
            triggered_by=triggered_by,
        )
        db.add(release)
        db.commit()
        db.refresh(release)
        db.commit()  # не держим транзакцию открытой на время сборки (и now() в БД не «замораживается» на её начале)

        stage = "build"
        if project.git_token_enc:     # приватный репозиторий: выдаём kpack доступ (токен хранится зашифрованно)
            kpack.ensure_git_credentials(project.slug, git_url, project.git_username or scm.GIT_USERNAME[project.provider],
                                         crypto.decrypt_value(project.git_token_enc), environment.build_namespace)
        if project.build_method == "dockerfile":
            new_image = kaniko.build_image(project, environment, git_url, revision)
        else:
            previous = kpack.get_status(environment.image_name, environment.build_namespace)["latest_image"]
            changed = kpack.trigger_build(environment.image_name, git_url, revision, project.sub_path, environment.build_namespace)
            if changed is False and previous:
                # та же ревизия: kpack ничего не пересоберёт — повторно выкатываем уже собранный образ (и секреты)
                new_image = previous
            else:
                new_image = kpack.wait_for_new_build(environment.image_name, previous, environment.build_namespace)

        stage = "deploy"
        # секреты сначала: под, который стартует с новым образом, уже получает актуальные значения
        targets.deploy(db, project, environment, new_image)

        release.image_digest = new_image
        release.status = "deployed"
        release.deployed_at = datetime.now(timezone.utc)  # время берём в коде: now() в Postgres = начало транзакции
        db.commit()

        audit.log_event(db, triggered_by, "deploy", project.id, environment.id,
                         {"image": new_image, "revision": revision})
    except Exception as e:  # любой сбой фиксируется в релизе и аудите, а не теряется в фоновом потоке
        logger.exception("build/deploy failed at stage %s", stage)
        db.rollback()
        if release is not None and project is not None and environment is not None:
            reason = getattr(e, "reason", None) or ("build_timeout" if isinstance(e, TimeoutError) else f"{stage}_error")
            _fail_release(db, release, project, environment, triggered_by, reason,
                          f"{type(e).__name__}: {e}", revision)
    finally:
        db.close()


def _dispatch_pull_request(provider: str, headers, payload: dict, background_tasks: BackgroundTasks) -> Optional[dict]:
    """События PR/MR передаются платному модулю «временные среды»; без него они игнорируются."""
    pr = scm.parse_pull_request(provider, headers, payload)
    if pr is None:
        return None
    from app import plugins
    if plugins.PULL_REQUEST_HANDLER is None:
        return {"skipped": True, "reason": "preview environments are not enabled"}
    if pr.get("action") is None:
        return {"skipped": True, "reason": "unhandled pull request action"}
    return plugins.PULL_REQUEST_HANDLER(pr, background_tasks)


def handle_push(db, project, pushes: list, background_tasks: BackgroundTasks) -> dict:
    """Общая часть для всех провайдеров: какие среды следят за веткой, выкатываем сразу или создаём заявку на согласование."""
    from app.services import approvals as approvals_svc
    if not pushes:
        return {"skipped": True, "reason": "no branch push in the event"}
    if not project_repo.is_routed(db, project):
        return {"skipped": True, "reason": "automatic deploys of several projects need the paid Projects module"}
    results = []
    for push in pushes:
        envs = project_repo.list_environments_by_branch(db, project, push.branch)
        if not envs:
            results.append({"skipped": True, "reason": f"no environment tracks branch {push.branch}"})
            continue
        auto = [e for e in envs if e.auto_deploy]
        if not auto:
            results.append({"skipped": True, "reason": f"auto_deploy disabled for environments tracking {push.branch}"})
            continue
        git_url = push.clone_url or project.clone_url
        started, approvals = [], []
        for environment in auto:      # все среды, следящие за веткой: одни выкатываются сразу, другие ждут согласования
            if environment.require_approval:
                a = approvals_svc.create(db, project, environment, "redeploy",
                                         {"revision": push.revision, "explicit_revision": True, "git_url": git_url}, "webhook", None)
                approvals.append({"environment": environment.name, "approval_id": str(a.id)})
            else:
                background_tasks.add_task(run_build_and_deploy, project.id, environment.id, git_url, push.revision, "webhook")
                started.append(environment.name)
        results.append({"accepted": True, "project": project.slug, "environment": auto[0].name, "revision": push.revision,
                        "environments": started, "requires_approval": bool(approvals), "approvals": approvals,
                        "approval_id": approvals[0]["approval_id"] if approvals else None})
    return results[0] if len(results) == 1 else {"accepted": any(r.get("accepted") for r in results), "results": results}


@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(default=None),
):
    raw_body = await request.body()
    _verify_signature(raw_body, x_hub_signature_256)
    payload = await request.json()
    pr = _dispatch_pull_request("github", request.headers, payload, background_tasks)
    if pr is not None:
        return pr
    pushes = scm.parse_push("github", request.headers, payload)
    if not pushes:
        ev = request.headers.get("x-github-event")
        return {"skipped": True, "reason": f"unhandled event {ev}" if ev != "push" else f"ignoring ref {payload.get('ref', '')}"}

    db = SessionLocal()
    try:
        project = project_repo.get_project_by_repo(db, pushes[0].repo, "github")
        if not project:
            raise HTTPException(status_code=404, detail=f"unknown project {pushes[0].repo}")
        return handle_push(db, project, pushes, background_tasks)
    finally:
        db.close()


@router.post("/{provider}/{slug}")
async def provider_webhook(provider: str, slug: str, request: Request, background_tasks: BackgroundTasks):
    """Вебхук GitLab / Bitbucket / Gitea: адрес содержит slug проекта, секрет у проекта свой (см. GET /api/projects/{slug}/webhook)."""
    if provider not in ("gitlab", "bitbucket", "gitea"):
        raise HTTPException(status_code=404, detail="unknown provider")
    raw = await request.body()
    if not scm.verify(provider, request.headers, raw, scm.webhook_secret(slug)):
        raise HTTPException(status_code=401, detail="invalid signature")
    payload = await request.json()
    db = SessionLocal()
    try:
        project = project_repo.get_project_by_slug(db, slug)
        if not project or project.provider != provider:
            raise HTTPException(status_code=404, detail=f"unknown project {slug}")
        pr = _dispatch_pull_request(provider, request.headers, payload, background_tasks)
        if pr is not None:
            return pr
        pushes = [p for p in scm.parse_push(provider, request.headers, payload) if p.repo == project.repo_full_name]
        if not pushes:
            return {"skipped": True, "reason": "event is not a push to a tracked branch of this repository"}
        return handle_push(db, project, pushes, background_tasks)
    finally:
        db.close()
