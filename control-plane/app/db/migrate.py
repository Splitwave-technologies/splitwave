import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]


def run_migrations() -> None:
    alembic_ini = BASE_DIR / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    logger.info("Running database migrations...")
    command.upgrade(cfg, "head")
    logger.info("Database migrations complete.")
