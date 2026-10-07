"""Разбор файла .env в пары «имя → значение» для импорта в секреты.

Поддерживается обычный синтаксис dotenv: комментарии (#), префикс export, значения в двойных кавычках (с экранированием \\n \\t \\\\ \\" и переносами
строк внутри) и в одинарных (буквально), комментарий после значения без кавычек (« #…»), пробелы вокруг «=». Подстановки ${VAR} НЕ раскрываются
(значение берётся как есть, ключ помечается notes=["interpolation"]). Пустые значения пропускаются — секрет не может быть пустым.

Правило безопасности: сообщения о проблемах содержат только номер строки, код причины и (для корректных имён) имя переменной — никогда значение."""
import re
from dataclasses import dataclass, field

from app.services import secrets as secrets_svc

MAX_TEXT = 256 * 1024
MAX_KEYS = 512

_LINE = re.compile(r"^\s*(?:export\s+)?([^=\s#][^=]*?)\s*=\s?(.*)$", re.S)
_ESC = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "$": "$", "'": "'"}


@dataclass
class Parsed:
    entries: dict = field(default_factory=dict)          # имя -> значение (последнее значение побеждает)
    notes: dict = field(default_factory=dict)            # имя -> ["duplicate", "interpolation", "multiline"]
    problems: list = field(default_factory=list)         # [{"line": n, "key": имя|None, "reason": код}]


def _unquote_double(rest: str):
    """rest — всё после открывающей кавычки. Возвращает (значение, остаток после закрывающей) или None, если кавычка не закрыта."""
    out, i = [], 0
    while i < len(rest):
        c = rest[i]
        if c == "\\" and i + 1 < len(rest):
            n = rest[i + 1]
            out.append(_ESC.get(n, "\\" + n)); i += 2; continue
        if c == '"':
            return "".join(out), rest[i + 1:]
        out.append(c); i += 1
    return None


def parse(text: str) -> Parsed:
    res = Parsed()
    if len(text.encode("utf-8", "replace")) > MAX_TEXT:
        res.problems.append({"line": 0, "key": None, "reason": "too_large"})
        return res
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw, no = lines[i], i + 1
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            res.problems.append({"line": no, "key": None, "reason": "bad_line"})
            continue
        key, rest = m.group(1).strip(), m.group(2).lstrip()
        try:
            secrets_svc.validate_key(key)
        except ValueError:
            res.problems.append({"line": no, "key": None, "reason": "bad_name"})
            continue
        notes = []
        if rest.startswith('"'):
            body = rest[1:]
            while True:
                got = _unquote_double(body)
                if got is not None or i >= len(lines):
                    break
                body += "\n" + lines[i]; i += 1; notes.append("multiline")
            if got is None:
                res.problems.append({"line": no, "key": key, "reason": "unterminated_quote"})
                break                                    # дальше файл разобрать нельзя: всё до конца — «внутри кавычки»
            value, tail = got
            if tail.strip() and not tail.lstrip().startswith("#"):
                res.problems.append({"line": no, "key": key, "reason": "text_after_quote"})
                continue
        elif rest.startswith("'"):
            body = rest[1:]
            while "'" not in body and i < len(lines):
                body += "\n" + lines[i]; i += 1; notes.append("multiline")
            if "'" not in body:
                res.problems.append({"line": no, "key": key, "reason": "unterminated_quote"})
                break
            value, tail = body.split("'", 1)
            if tail.strip() and not tail.lstrip().startswith("#"):
                res.problems.append({"line": no, "key": key, "reason": "text_after_quote"})
                continue
        else:
            value = re.split(r"\s#", rest, maxsplit=1)[0].rstrip()
        if "${" in value:
            notes.append("interpolation")
        if value == "":
            res.problems.append({"line": no, "key": key, "reason": "empty"})
            continue
        try:
            secrets_svc.validate_value(value)
        except ValueError:
            res.problems.append({"line": no, "key": key, "reason": "value_too_large"})
            continue
        if key in res.entries:
            notes.append("duplicate")
        elif len(res.entries) >= MAX_KEYS:
            res.problems.append({"line": no, "key": key, "reason": "too_many_keys"})
            continue
        res.entries[key] = value
        res.notes[key] = sorted(set(notes) | ({"duplicate"} if key in res.notes and "duplicate" in notes else set()))
    return res
