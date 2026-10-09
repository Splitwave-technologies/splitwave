#!/usr/bin/env python3
"""Список зависимостей и тексты их лицензий из УСТАНОВЛЕННЫХ пакетов текущего окружения (внешние утилиты не нужны).

    python scripts/gen_licenses.py --out licenses --json licenses/third-party.json

Создаёт: <out>/THIRD-PARTY.md (таблица: пакет, версия, лицензия, сайт), <out>/<пакет>-<версия>/ с файлами LICENSE/COPYING/NOTICE
(полные тексты лицензий и NOTICE-файлы, как требуют Apache-2.0, LGPL-3.0 и др.), а при --json ещё и машиночитаемый список.
Запускайте в окружении, где установлены только рабочие зависимости (например, в образе или в чистом venv)."""
import argparse, json, re, shutil, sys
from importlib import metadata
from pathlib import Path

LIC_FILE = re.compile(r"(?i)(^|/)(licen[sc]e|copying|copyright|notice|authors)[^/]*$")
SKIP = {"pip", "setuptools", "wheel", "pip-licenses", "pkg-resources", "distribute"}


def license_name(md) -> str:
    expr = (md.get("License-Expression") or "").strip()
    if expr:
        return expr
    lic = (md.get("License") or "").strip()
    if lic and len(lic) < 80 and "\n" not in lic and lic.upper() != "UNKNOWN":
        return lic
    cls = [c.split("::")[-1].strip() for c in (md.get_all("Classifier") or []) if c.startswith("License ::")]
    cls = [c for c in cls if c != "OSI Approved"]
    if cls:
        return " / ".join(cls)
    return lic.splitlines()[0][:60] if lic else "UNKNOWN"


def project_url(md) -> str:
    if md.get("Home-page"):
        return md["Home-page"]
    for v in md.get_all("Project-URL") or []:
        label, _, url = v.partition(",")
        if label.strip().lower() in ("homepage", "home", "source", "repository", "documentation"):
            return url.strip()
    return ((md.get_all("Project-URL") or [""])[0].partition(",")[2]).strip()


def collect():
    out, seen = [], set()
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        md = dist.metadata
        name = md["Name"]
        if not name or name.lower() in SKIP or (name.lower(), dist.version) in seen:      # lib и lib64 в venv дают дубли
            continue
        seen.add((name.lower(), dist.version))
        files = [f for f in (dist.files or []) if LIC_FILE.search(str(f).replace("\\", "/")) and ".dist-info" in str(f) or LIC_FILE.search(str(f).replace("\\", "/")) and ".egg-info" in str(f)]
        out.append({"name": name, "version": dist.version, "license": license_name(md), "url": project_url(md), "dist": dist, "files": files})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="licenses")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    pkgs = collect()
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    rows, missing = [], []
    for p in pkgs:
        d = out / f"{p['name']}-{p['version']}"
        n = 0
        for f in p["files"]:
            src = Path(p["dist"].locate_file(f))
            if src.is_file():
                d.mkdir(exist_ok=True)
                shutil.copyfile(src, d / src.name)
                n += 1
        if n == 0:
            missing.append(p["name"])
        rows.append({"name": p["name"], "version": p["version"], "license": p["license"], "url": p["url"], "license_files": n})
    md = ["# Third-party software included in SplitWave", "",
          "Complete texts of the licences and NOTICE files are in the folders next to this file. Each component remains under its own licence.", "",
          "| Component | Version | Licence | Source |", "|---|---|---|---|"]
    md += [f"| {r['name']} | {r['version']} | {r['license']} | {r['url']} |" for r in rows]
    (out / "THIRD-PARTY.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"packages: {len(rows)}; without licence file: {missing or 'none'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
