"""Скрипт установки на один сервер: синтаксис и справка (сам запуск требует root, чистый сервер и интернет — проверен на реальном VPS)."""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/quickstart/install-single-node.sh"
pytestmark = pytest.mark.skipif(not SCRIPT.exists() or not shutil.which("bash"), reason="нужен каталог deploy и bash")


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0


def test_help_documents_https_and_offline_image_options():
    out = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert out.returncode == 0
    for flag in ("--tls auto|custom|none", "--email", "--tls-cert", "--tls-key", "--image-archive", "--host"):
        assert flag in out.stdout, flag
    assert "Let's Encrypt" in out.stdout


def test_unknown_arguments_are_rejected():
    out = subprocess.run(["bash", str(SCRIPT), "--no-such-flag"], capture_output=True, text=True)
    assert out.returncode == 2 and "unknown argument" in out.stdout
