#!/usr/bin/env python3
"""One-time loopback setup. Tokens are accepted only in a password form POST."""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse, request

import certifi

MAX_REQUEST_BYTES = 4096
TOKEN_PATTERN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,160}\Z")
EXPECTED_USERNAME = "doatapexambot"


def validate_bot_token(token: str) -> bool:
    """Check ownership through getMe. Never expose network exception details."""
    if not TOKEN_PATTERN.fullmatch(token):
        return False
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        check = request.Request(f"https://api.telegram.org/bot{token}/getMe", method="GET")
        with request.urlopen(check, timeout=15, context=context) as response:
            body = response.read(65537)
            if len(body) > 65536:
                return False
        data = json.loads(body)
        result = data.get("result", {})
        return (data.get("ok") is True and result.get("is_bot") is True
                and str(result.get("username", "")).lower() == EXPECTED_USERNAME)
    except Exception:
        return False


def private_file(path: Path, *, exclusive: bool = False) -> int:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_APPEND)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return descriptor


class LocalSetup:
    def __init__(self, root: Path, bank: Path, *, validator=None, launcher=None):
        self.root = root.resolve()
        self.bank = bank.resolve()
        self.path = "/setup/" + secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.validator = validator or validate_bot_token
        self.launcher = launcher or subprocess.Popen
        self.lock = threading.Lock()
        self.process = None
        self.launched_at = None
        self.attempted_launch = False
        self.failed = False

    def status(self) -> str:
        if self.failed:
            return "failed"
        if self.process is None:
            return "waiting"
        if self.process.poll() is not None:
            return "failed"
        return "running" if time.monotonic() - self.launched_at >= 1 else "starting"

    def start(self, token: str) -> tuple[bool, str]:
        with self.lock:
            if self.attempted_launch:
                return True, ""
            env_path = self.root / ".env"
            if env_path.exists() or env_path.is_symlink():
                return False, "Файл .env уже существует. Он не изменён. Запустите бота с сохранённой конфигурацией."
            if not TOKEN_PATTERN.fullmatch(token) or not self.validator(token):
                return False, "Не удалось подтвердить токен @DOATAPEXAMBOT. Проверьте токен и доступ к интернету."
            if not self.bank.is_file() or not (self.root / ".venv/bin/python").is_file():
                return False, "Локальная среда или банк вопросов не готовы. Конфигурация не сохранена."
            output = self.root / "output/local"
            try:
                output.mkdir(parents=True, exist_ok=True)
                (self.root / "runtime").mkdir(exist_ok=True)
                # Exclusive creation also prevents two setup server instances
                # from overwriting credentials or starting duplicate pollers.
                bank_value = str(self.bank).replace("\\", "\\\\").replace('"', '\\"')
                if "\n" in bank_value or "\r" in bank_value:
                    return False, "Путь к банку вопросов недопустим."
                contents = (f"BOT_TOKEN={token}\nDATABASE_URL=sqlite:///runtime/doatap.db\n"
                            f'QUESTION_BANK_PATH="{bank_value}"\n')
                with os.fdopen(private_file(env_path, exclusive=True), "w", encoding="utf-8") as target:
                    target.write(contents)
                    target.flush()
                    os.fsync(target.fileno())
                self.attempted_launch = True
                child_env = os.environ.copy()
                for key in ("BOT_TOKEN", "DATABASE_URL", "QUESTION_BANK_PATH", "PYTHON_DOTENV_DISABLED"):
                    child_env.pop(key, None)
                child_env["PYTHONUNBUFFERED"] = "1"
                with os.fdopen(private_file(output / "bot.log"), "ab") as log:
                    self.process = self.launcher(
                        [str(self.root / ".venv/bin/python"), "-m", "doatap", "run"],
                        cwd=self.root, env=child_env, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True, close_fds=True)
                self.launched_at = time.monotonic()
                # Replace only a stale PID marker; it contains no credentials.
                pid_path = output / "bot.pid"
                descriptor = private_file(pid_path)
                with os.fdopen(descriptor, "w", encoding="ascii") as target:
                    target.seek(0)
                    target.truncate()
                    target.write(str(self.process.pid) + "\n")
                return True, ""
            except FileExistsError:
                return False, "Файл .env уже существует. Он не изменён; повторный процесс не запущен."
            except Exception:
                self.failed = True
                # If bookkeeping fails, do not leave an untracked polling bot.
                if self.process is not None and self.process.poll() is None:
                    self.process.terminate()
                return False, "Не удалось запустить локальный процесс. Сохранённую конфигурацию можно проверить в .env."


def page(body: str, *, refresh: bool = False) -> bytes:
    refresh_tag = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return ("<!doctype html><html lang=ru><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"{refresh_tag}<title>DOATAP · Локальный запуск</title>"
            "<style>body{font:17px system-ui,sans-serif;max-width:640px;margin:8vh auto;padding:24px;color:#142d42;"
            "background:#f5f8fa}main{padding:32px;background:white;border-radius:18px}h1{font-size:27px}"
            "input,button{box-sizing:border-box;width:100%;padding:14px;margin:12px 0;font:inherit;border-radius:8px}"
            "input{border:1px solid #aac0ce}button{border:0;background:#16677b;color:white;cursor:pointer}"
            ".note{font-size:14px;color:#456172}.error{color:#9b2c25}a{color:#16677b}</style>"
            f"<main>{body}</main></html>").encode("utf-8")


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, setup: LocalSetup):
        self.setup = setup
        super().__init__(("127.0.0.1", 0), SetupHandler)
        self.expected_host = f"127.0.0.1:{self.server_port}"
        self.origin = "http://" + self.expected_host

    def handle_error(self, request_socket, client_address):
        # A dropped browser connection must never dump request context.
        pass


class SetupHandler(BaseHTTPRequestHandler):
    server_version = "LocalSetup"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        pass

    def respond(self, status: int, body: bytes, *, location: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        if location:
            self.send_header("Location", location)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def valid_source(self, *, post=False):
        if self.headers.get("Host") != self.server.expected_host:
            return False
        origin = self.headers.get("Origin")
        return origin == self.server.origin if post or origin else True

    def form(self, error=""):
        setup = self.server.setup
        notice = f'<p class="error">{html.escape(error)}</p>' if error else ""
        return page(
            "<h1>Запуск @DOATAPEXAMBOT</h1>"
            "<p>Введите токен из BotFather. Он проверяется через Telegram и сохраняется только в локальном .env.</p>"
            f"{notice}<form method=post action='{setup.path}'>"
            f"<input type=hidden name=csrf value='{setup.csrf}'>"
            "<label for=token>Токен бота</label>"
            "<input id=token name=token type=password autocomplete=new-password spellcheck=false maxlength=200 required>"
            "<button type=submit>Проверить и запустить</button></form>"
            "<p class=note>Страница доступна только на этом Mac. Токен не нужно отправлять в чат. "
            "Локальный бот работает, пока Mac включён и не спит.</p>")

    def status_page(self):
        state = self.server.setup.status()
        messages = {
            "waiting": ("Ожидание настройки", "Процесс бота ещё не запущен."),
            "starting": ("Начинается локальный запуск", "Токен проверен. Процесс создан; проверяем, что он продолжает работать."),
            "running": ("Бот запущен локально", "Процесс работает. Откройте @DOATAPEXAMBOT в Telegram и отправьте /start для проверки связи."),
            "failed": ("Локальный процесс остановлен", "Связь с ботом не подтверждена. Проверьте локальный журнал output/local/bot.log. Токен повторно вводить не нужно."),
        }
        title, text = messages[state]
        return page(f"<h1>{title}</h1><p id=state data-state='{state}'>{text}</p>"
                    "<p class=note>Mac должен оставаться включённым и не переходить в сон. "
                    "Закрытие этой страницы не останавливает уже запущенного бота.</p>"
                    f"<p><a href='{self.server.setup.path}/status'>Обновить статус процесса</a></p>",
                    refresh=state in {"starting", "running"})

    def do_GET(self):
        if not self.valid_source():
            self.respond(403, page("<p>Запрос отклонён.</p>"))
        elif self.path == self.server.setup.path:
            self.respond(200, self.status_page() if self.server.setup.attempted_launch else self.form())
        elif self.path == self.server.setup.path + "/status":
            self.respond(200, self.status_page())
        else:
            self.respond(404, page("<p>Страница не найдена.</p>"))

    def do_POST(self):
        if not self.valid_source(post=True) or self.path != self.server.setup.path:
            self.respond(403, page("<p>Запрос отклонён.</p>"))
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if self.headers.get("Transfer-Encoding") or length < 0 or length > MAX_REQUEST_BYTES:
                self.respond(413, page("<p>Запрос отклонён.</p>"))
                return
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.respond(415, page("<p>Запрос отклонён.</p>"))
                return
            fields = parse.parse_qs(self.rfile.read(length).decode("utf-8"),
                                    keep_blank_values=True, max_num_fields=4, strict_parsing=True)
            csrf = fields.get("csrf", [])
            tokens = fields.get("token", [])
            if len(csrf) != 1 or not secrets.compare_digest(csrf[0], self.server.setup.csrf):
                self.respond(403, page("<p>Запрос отклонён. Обновите страницу настройки.</p>"))
                return
            if len(tokens) != 1 or set(fields) != {"csrf", "token"}:
                raise ValueError("Invalid form")
            success, error = self.server.setup.start(tokens[0].strip())
            if success:
                # POST/Redirect/GET removes the credential POST from refreshes.
                self.respond(303, page("<p>Переход к статусу локального процесса.</p>"),
                             location=self.server.setup.path + "/status")
            else:
                self.respond(400, self.form(error))
        except Exception:
            self.respond(400, page("<p>Не удалось обработать запрос. Обновите страницу настройки.</p>"))


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Local one-time DOATAP setup")
    parser.add_argument("--bank", type=Path, default=root / "data/questions.jsonl")
    args = parser.parse_args()
    server = SetupServer(LocalSetup(root, args.bank))
    print(server.origin + server.setup.path, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
