"""Dockerfile, который платформа предлагает проекту без Dockerfile.

По определённому стеку (язык, менеджер пакетов, скрипты, точка входа, порт) собирается обычный многоэтапный Dockerfile без привилегий
(пользователь без root, переменная PORT). Пользователь видит текст в мастере, правит его и параметры; платформа хранит итог у себя
(projects.dockerfile_content) и передаёт сборке kaniko — в репозиторий ничего не пишется.

Параметры (port, start_command, runtime_version) проходят строгую проверку, потому что попадают в текст Dockerfile.
Шаблоны рассчитаны на типовые проекты; всё нестандартное мастер честно отмечает в notes, а пользователь дописывает вручную."""
import json
import re
from typing import Optional

MAX_CONTENT = 16 * 1024
SUPPORTED = ("node", "python", "go", "java", "php", "ruby", "dotnet", "rust", "static")
VERSION_RE = re.compile(r"^[0-9][0-9.]{0,7}$")
CMD_BAD = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")             # без переводов строк и управляющих символов (допускается табуляция)
DEFAULT_VERSION = {"node": "20", "python": "3.12", "go": "1.22", "java": "21", "php": "8.3", "ruby": "3.3", "dotnet": "8.0", "rust": "1"}
DEFAULT_PORT = {"node": 3000, "python": 8000, "go": 8080, "java": 8080, "php": 8080, "ruby": 3000, "dotnet": 8080, "rust": 8080, "static": 8080}

NGINX_SPA_CONF = ("server { listen 8080; root /usr/share/nginx/html; index index.html; "
                  "location / { try_files $uri $uri/ /index.html; } }")


class DockerfileError(ValueError):
    pass


def validate_content(text: str) -> str:
    """Текст Dockerfile, который вводит пользователь: размер, без NUL, есть хотя бы один FROM."""
    text = (text or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        raise DockerfileError("Dockerfile пуст")
    if len(text.encode()) > MAX_CONTENT:
        raise DockerfileError(f"Dockerfile больше {MAX_CONTENT // 1024} КБ")
    if "\x00" in text:
        raise DockerfileError("Dockerfile содержит недопустимые символы")
    if not re.search(r"(?im)^\s*FROM\s+\S+", text):
        raise DockerfileError("в Dockerfile нет инструкции FROM")
    return text + "\n"


def _version(params: dict, language: str, facts: dict) -> str:
    v = str(params.get("runtime_version") or facts.get("runtime_version") or DEFAULT_VERSION.get(language, ""))
    if language in DEFAULT_VERSION and not VERSION_RE.match(v):
        raise DockerfileError("версия среды выполнения: цифры и точки, например 20 или 3.12")
    return v


def _port(params: dict, default: int) -> int:
    try:
        raw = params.get("port")
        p = int(default if raw in (None, "") else raw)
    except (TypeError, ValueError):
        raise DockerfileError("порт: число от 1 до 65535")
    if not 1 <= p <= 65535:
        raise DockerfileError("порт: число от 1 до 65535")
    return p


def _command(params: dict, default: str) -> str:
    c = str(params.get("start_command") if params.get("start_command") not in (None, "") else default).strip()
    if not c or len(c) > 300 or CMD_BAD.search(c):
        raise DockerfileError("команда запуска: одна строка до 300 символов")
    return c


def _cmd_line(cmd: str) -> str:
    return f'CMD ["sh", "-c", {json.dumps(cmd, ensure_ascii=False)}]'


# ---------- шаблоны ----------

def _node(facts: dict, params: dict) -> tuple[str, list[str], dict]:
    notes: list[str] = []
    v = _version(params, "node", facts)
    pm = facts.get("package_manager", "npm")
    copy_lock = "COPY package.json package-lock.json* yarn.lock* pnpm-lock.yaml* ./"
    corepack = "corepack enable && " if pm in ("yarn", "pnpm") else ""
    install_all = {"npm": "npm ci" if facts.get("has_lock") else "npm install",
                   "yarn": "yarn install --frozen-lockfile" if facts.get("has_lock") else "yarn install",
                   "pnpm": "pnpm install --frozen-lockfile" if facts.get("has_lock") else "pnpm install"}[pm]
    build = {"npm": "npm run build", "yarn": "yarn build", "pnpm": "pnpm build"}[pm]
    if facts.get("static_site"):                                      # фронтенд без сервера: сборка + nginx
        out = str(facts.get("build_output") or "dist")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,60}", out) or ".." in out.split("/"):
            raise DockerfileError("каталог результата сборки: например dist или build")
        if facts.get("build_output_unknown"):
            notes.append("Каталог результата сборки определить не удалось: проверьте строку COPY --from=build (dist, build или dist/<имя>).")
        text = "\n".join([
            f"FROM node:{v}-alpine AS build", "WORKDIR /app", copy_lock, f"RUN {corepack}{install_all}", "COPY . .", f"RUN {corepack}{build}", "",
            "FROM nginxinc/nginx-unprivileged:1.27-alpine", "USER root",
            f"RUN printf '%s\\n' '{NGINX_SPA_CONF}' > /etc/nginx/conf.d/default.conf",
            f"COPY --from=build /app/{out} /usr/share/nginx/html", "USER nginx", "EXPOSE 8080", ""])
        notes.append("Статический сайт отдаётся через nginx на порту 8080 (переход на index.html включён для одностраничных приложений).")
        return text, notes, {"port": 8080, "runtime_version": v}
    port = _port(params, facts.get("port") or DEFAULT_PORT["node"])
    start = facts.get("start_command") or ""
    default_cmd = {"npm": "npm start", "yarn": "yarn start", "pnpm": "pnpm start"}[pm] if facts.get("has_start") else (f"node {start}" if start else "node index.js")
    cmd = _command(params, default_cmd)
    if not facts.get("has_start"):
        notes.append(f"В package.json нет скрипта start: запуск через «{cmd}» — проверьте команду запуска.")
    lines = [f"FROM node:{v}-alpine", "WORKDIR /app", "ENV NODE_ENV=production", f"ENV PORT={port}", copy_lock]
    if facts.get("has_build"):
        lines += [f"RUN {corepack}NODE_ENV=development {install_all}", "COPY . .", f"RUN {corepack}{build}"]
        if pm == "npm":
            lines.append("RUN npm prune --omit=dev")
    else:
        prod = {"npm": install_all + " --omit=dev" if facts.get("has_lock") else "npm install --omit=dev", "yarn": install_all + " --production", "pnpm": install_all + " --prod"}[pm]
        lines += [f"RUN {corepack}{prod}", "COPY . ."]
    lines += ["RUN chown -R node:node /app", "USER node", f"EXPOSE {port}", _cmd_line(cmd), ""]
    return "\n".join(lines), notes, {"port": port, "start_command": cmd, "runtime_version": v}


def _python(facts: dict, params: dict) -> tuple[str, list[str], dict]:
    notes: list[str] = []
    v = _version(params, "python", facts)
    port = _port(params, facts.get("port") or DEFAULT_PORT["python"])
    default_cmd = facts.get("start_command") or "python main.py"
    cmd = _command(params, default_cmd)
    if facts.get("start_guess"):
        notes.append(f"Команда запуска определена по эвристике («{cmd}»): проверьте имя модуля и приложения.")
    lines = [f"FROM python:{v}-slim", "WORKDIR /app", "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1", f"ENV PORT={port}"]
    deps = facts.get("deps")
    if deps == "requirements":
        lines += ["COPY requirements.txt ./", "RUN pip install --no-cache-dir -r requirements.txt"]
    elif deps == "pipfile":
        lines += ["RUN pip install --no-cache-dir pipenv", "COPY Pipfile Pipfile.lock* ./", "RUN pipenv install --system --deploy --ignore-pipfile || pipenv install --system"]
    for extra in facts.get("extra_packages", []):
        if re.fullmatch(r"[A-Za-z0-9._-]{1,40}", extra):
            lines.append(f"RUN pip install --no-cache-dir {extra}")
    lines.append("COPY . .")
    if deps == "pyproject":
        lines.append("RUN pip install --no-cache-dir .")
    if not deps:
        notes.append("Файл зависимостей (requirements.txt / pyproject.toml / Pipfile) не найден: зависимости не устанавливаются.")
    lines += ["RUN useradd -r -u 10001 app && chown -R app /app", "USER app", f"EXPOSE {port}", _cmd_line(cmd), ""]
    return "\n".join(lines), notes, {"port": port, "start_command": cmd, "runtime_version": v}


def _go(facts: dict, params: dict) -> tuple[str, list[str], dict]:
    notes: list[str] = []
    v = _version(params, "go", facts)
    port = _port(params, facts.get("port") or DEFAULT_PORT["go"])
    pkg = facts.get("main_package") or "."
    if not re.fullmatch(r"\.(/[A-Za-z0-9._/-]{1,80})?", pkg) or ".." in pkg.split("/"):
        raise DockerfileError("пакет main: путь вида . или ./cmd/server")
    if facts.get("main_unknown"):
        notes.append("Пакет main определить не удалось (несколько команд или нет main.go в корне): проверьте путь в go build.")
    text = "\n".join([
        f"FROM golang:{v}-alpine AS build", "WORKDIR /src", "COPY go.mod go.sum* ./", "RUN go mod download", "COPY . .",
        f"RUN CGO_ENABLED=0 go build -o /out/app {pkg}", "",
        "FROM alpine:3.20", "RUN apk add --no-cache ca-certificates tzdata && adduser -D -u 10001 app",
        "WORKDIR /app", "COPY --from=build /src /app", "COPY --from=build /out/app /usr/local/bin/app", "USER app", f"ENV PORT={port}", f"EXPOSE {port}", 'CMD ["app"]', ""])
    notes.append("Приложение должно слушать порт из переменной окружения PORT (или тот же порт, что указан здесь).")
    notes.append("Рядом с программой в образ копируется исходное дерево: шаблоны, статика и конфигурация, которые приложение читает при запуске.")
    return text, notes, {"port": port, "runtime_version": v}


def _java(facts: dict, params: dict) -> tuple[str, list[str], dict]:
    notes: list[str] = []
    v = _version(params, "java", facts)
    port = _port(params, facts.get("port") or DEFAULT_PORT["java"])
    if facts.get("build_tool") == "gradle":
        build_img = f"gradle:8-jdk{v}"
        build = ("RUN (chmod +x gradlew && ./gradlew --no-daemon -q build -x test) || gradle --no-daemon -q build -x test && "
                 'cp "$(ls build/libs/*.jar | grep -v plain | head -1)" /app.jar')
    else:
        build_img = f"maven:3.9-eclipse-temurin-{v}"
        build = 'RUN mvn -q -B package -DskipTests && cp "$(ls target/*.jar | grep -v original | head -1)" /app.jar'
    cmd = _command(params, "java -Dserver.port=$PORT -jar /app.jar")
    text = "\n".join([
        f"FROM {build_img} AS build", "WORKDIR /src", "COPY . .", build, "",
        f"FROM eclipse-temurin:{v}-jre", "RUN useradd -r -u 10001 app", "COPY --from=build /app.jar /app.jar", "USER app",
        f"ENV PORT={port}", f"EXPOSE {port}", _cmd_line(cmd), ""])
    notes.append("Собирается исполняемый jar (Spring Boot и подобные); для war/нестандартной сборки отредактируйте шаги сборки.")
    return text, notes, {"port": port, "start_command": cmd, "runtime_version": v}


def _php(facts: dict, params: dict) -> tuple[str, list[str], dict]:
    v = _version(params, "php", facts)
    port = _port(params, facts.get("port") or DEFAULT_PORT["php"])
    docroot = str(facts.get("docroot") or "")
    if docroot and not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", docroot):
        raise DockerfileError("корень сайта: имя папки")
    root_path = f"/var/www/html/{docroot}" if docroot else "/var/www/html"
    text = "\n".join([
        "FROM composer:2 AS vendor", "WORKDIR /app", "COPY . .",
        "RUN if [ -f composer.json ]; then composer install --no-dev --no-interaction --prefer-dist --optimize-autoloader --ignore-platform-reqs; fi", "",
        f"FROM php:{v}-apache",
        "RUN docker-php-ext-install pdo_mysql mysqli && a2enmod rewrite headers && echo 'ServerName localhost' > /etc/apache2/conf-available/servername.conf && a2enconf servername \\",
        f" && sed -ri 's/Listen 80/Listen {port}/' /etc/apache2/ports.conf && sed -ri 's/:80>/:{port}>/' /etc/apache2/sites-available/000-default.conf \\",
        f" && sed -ri 's#DocumentRoot /var/www/html#DocumentRoot {root_path}#' /etc/apache2/sites-available/000-default.conf \\",
        " && sed -ri '/<Directory \\/var\\/www\\/>/,/<\\/Directory>/ s/AllowOverride None/AllowOverride All/' /etc/apache2/apache2.conf \\",
        " && mkdir -p /var/run/apache2 /var/lock/apache2 /var/log/apache2 && chown -R www-data:www-data /var/run/apache2 /var/lock/apache2 /var/log/apache2",
        "COPY --from=vendor --chown=www-data:www-data /app /var/www/html", "USER www-data", f"ENV PORT={port}", f"EXPOSE {port}", 'CMD ["apache2-foreground"]', ""])
    notes = ["Приложение работает под Apache (php-apache); mod_rewrite включён, .htaccess учитывается.",
             "Подключены расширения pdo_mysql и mysqli; для других (pgsql, gd, intl и т.д.) добавьте docker-php-ext-install в Dockerfile."]
    if docroot:
        notes.append(f"Корень сайта: «{docroot}» (по структуре репозитория).")
    return text, notes, {"port": port, "runtime_version": v}


def _ruby(facts: dict, params: dict) -> tuple[str, list[str], dict]:
    v = _version(params, "ruby", facts)
    port = _port(params, facts.get("port") or DEFAULT_PORT["ruby"])
    default_cmd = "bundle exec rails server -b 0.0.0.0 -p $PORT" if facts.get("framework") == "rails" else (facts.get("procfile_web") or "bundle exec rackup -o 0.0.0.0 -p $PORT")
    cmd = _command(params, default_cmd)
    text = "\n".join([
        f"FROM ruby:{v}-slim AS build",
        "RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libyaml-dev git && rm -rf /var/lib/apt/lists/*",
        "WORKDIR /app", "COPY Gemfile Gemfile.lock* ./", "RUN bundle config set --local without 'development test' && bundle install", "COPY . .", "",
        f"FROM ruby:{v}-slim",
        "RUN apt-get update && apt-get install -y --no-install-recommends libpq5 libyaml-0-2 && rm -rf /var/lib/apt/lists/* && useradd -r -u 10001 app",
        "WORKDIR /app", "COPY --from=build /usr/local/bundle /usr/local/bundle", "COPY --from=build --chown=10001:10001 /app /app",
        f"ENV RAILS_ENV=production RACK_ENV=production RAILS_LOG_TO_STDOUT=1 RAILS_SERVE_STATIC_FILES=1 PORT={port}", "USER app", f"EXPOSE {port}", _cmd_line(cmd), ""])
    notes = ["Gemfile и Gemfile.lock должны быть в корне; сборка идёт без групп development и test."]
    if facts.get("framework") == "rails":
        notes.append("Для Rails задайте в «Секретах» SECRET_KEY_BASE (и DATABASE_URL, если нужна база); миграции платформа сама не запускает.")
    return text, notes, {"port": port, "start_command": cmd, "runtime_version": v}


def _dotnet(facts: dict, params: dict) -> tuple[str, list[str], dict]:
    v = _version(params, "dotnet", facts)
    port = _port(params, facts.get("port") or DEFAULT_PORT["dotnet"])
    proj = str(facts.get("project") or "")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}\.(cs|fs)proj", proj) or ".." in proj.split("/"):
        raise DockerfileError("проект .NET: путь к .csproj")
    dll = proj.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".dll"
    text = "\n".join([
        f"FROM mcr.microsoft.com/dotnet/sdk:{v} AS build", "WORKDIR /src", "COPY . .", f"RUN dotnet publish {proj} -c Release -o /out", "",
        f"FROM mcr.microsoft.com/dotnet/aspnet:{v}", "WORKDIR /app", "COPY --from=build /out .",
        f"ENV ASPNETCORE_URLS=http://0.0.0.0:{port} PORT={port} DOTNET_RUNNING_IN_CONTAINER=true", "USER app", f"EXPOSE {port}", f'ENTRYPOINT ["dotnet", "{dll}"]', ""])
    return text, [f"Публикуется проект {proj}; запускается {dll}. Для нестандартного имени сборки поправьте ENTRYPOINT."], {"port": port, "runtime_version": v}


def _rust(facts: dict, params: dict) -> tuple[str, list[str], dict]:
    v = _version(params, "rust", facts)
    port = _port(params, facts.get("port") or DEFAULT_PORT["rust"])
    binary = str(facts.get("binary") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", binary):
        raise DockerfileError("имя программы Rust: как в Cargo.toml (name)")
    text = "\n".join([
        f"FROM rust:{v}-slim AS build", "WORKDIR /src", "COPY . .", "RUN cargo build --release", "",
        "FROM debian:bookworm-slim", "RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/* && useradd -r -u 10001 app",
        "WORKDIR /app", f"COPY --from=build /src/target/release/{binary} /usr/local/bin/app", "USER app", f"ENV PORT={port}", f"EXPOSE {port}", 'CMD ["app"]', ""])
    return text, ["Приложение должно слушать порт из переменной окружения PORT.", "Первая сборка Rust долгая: зависимости компилируются с нуля."], {"port": port, "runtime_version": v}


def _static(facts: dict, params: dict) -> tuple[str, list[str], dict]:
    text = "\n".join(["FROM nginxinc/nginx-unprivileged:1.27-alpine", "COPY --chown=nginx:nginx . /usr/share/nginx/html", "USER nginx", "EXPOSE 8080", ""])
    return text, ["Статический сайт отдаётся через nginx на порту 8080."], {"port": 8080}


_RENDER = {"node": _node, "python": _python, "go": _go, "java": _java, "php": _php, "ruby": _ruby, "dotnet": _dotnet, "rust": _rust, "static": _static}


def render(language: str, facts: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    """{content, notes, params}. DockerfileError — параметры недопустимы или для языка нет шаблона."""
    if language not in _RENDER:
        raise DockerfileError(f"для языка «{language}» нет готового шаблона")
    try:
        content, notes, used = _RENDER[language](facts or {}, params or {})
    except DockerfileError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError):       # факты приходят от клиента мастера: мусор не должен давать 500
        raise DockerfileError("недопустимые параметры шаблона")
    return {"content": validate_content(content), "notes": notes, "params": used}
