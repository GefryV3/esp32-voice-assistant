"""Один раз войти в Google и получить refresh-токен для сервера.

    python tools/google_auth.py

Нужны GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET в server/.env (OAuth-клиент типа «Desktop app»,
инструкция в docs/google.md). Скрипт откроет браузер, ты входишь в аккаунт и разрешаешь доступ
(Календарь, Gmail только чтение, Задачи); токен записывается в server/.env строкой
GOOGLE_REFRESH_TOKEN, в вывод и в чат он не попадает.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from jarvis.config import read_env_file  # noqa: E402
from jarvis.google import SCOPES  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ENV = SERVER / ".env"


def save_token(token: str) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    lines = [ln for ln in lines if not ln.startswith("GOOGLE_REFRESH_TOKEN=")]
    lines.append(f"GOOGLE_REFRESH_TOKEN={token}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    env = read_env_file(ENV)
    cid, secret = env.get("GOOGLE_CLIENT_ID", ""), env.get("GOOGLE_CLIENT_SECRET", "")
    if not cid or not secret:
        sys.exit("в server/.env нет GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET (см. docs/google.md)")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state and "code" in q:
                got["code"] = q["code"]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Готово, это окно можно закрыть.</h2>".encode() if "code" in got
                             else "<h2>Что-то не так, вернись в терминал.</h2>".encode())
            done.set()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("Открываю браузер. Если не открылся, перейди по ссылке:\n" + url)
    webbrowser.open(url)
    if not done.wait(300) or "code" not in got:
        sys.exit("вход не завершён за 5 минут")
    server.shutdown()

    data = urllib.parse.urlencode({
        "client_id": cid, "client_secret": secret, "code": got["code"], "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=20) as r:
            reply = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Google отказал: {e.read().decode()[:300]}")
    if "refresh_token" not in reply:
        sys.exit("Google не вернул refresh-токен: отзови доступ приложению на myaccount.google.com/permissions и повтори")
    save_token(reply["refresh_token"])
    print("Готово: GOOGLE_REFRESH_TOKEN записан в server/.env. Доступ:", reply.get("scope", ""))


if __name__ == "__main__":
    main()
