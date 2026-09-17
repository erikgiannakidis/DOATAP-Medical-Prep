import importlib.util
import io
import json
from email.message import Message
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock
from urllib import parse

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/local_setup.py"
SPEC = importlib.util.spec_from_file_location("local_setup", MODULE_PATH)
local_setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_setup)
TOKEN = "123456789:" + "T" * 35


@pytest.fixture
def setup(tmp_path):
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    bank = tmp_path / "data/questions.jsonl"
    bank.parent.mkdir()
    bank.write_text("{}\n", encoding="utf-8")
    process = SimpleNamespace(pid=12345, poll=Mock(return_value=None), terminate=Mock())
    return local_setup.LocalSetup(tmp_path, bank, validator=Mock(return_value=True),
                                  launcher=Mock(return_value=process))


@pytest.fixture
def server(setup, monkeypatch):
    # Exercise HTTP handlers without opening sockets; CI may deny loopback binds.
    def init(server, address, handler):
        assert address == ("127.0.0.1", 0)
        assert handler is local_setup.SetupHandler
        server.server_port = 12345
        server.server_address = ("127.0.0.1", 12345)

    monkeypatch.setattr(local_setup.ThreadingHTTPServer, "__init__", init)
    return local_setup.SetupServer(setup)


def handle(server, method, *, path=None, data=b"", headers=None):
    handler = local_setup.SetupHandler.__new__(local_setup.SetupHandler)
    handler.server = server
    handler.path = path or server.setup.path
    handler.headers = Message()
    values = {"Host": server.expected_host, "Content-Length": str(len(data))}
    values.update(headers or {})
    for name, value in values.items():
        handler.headers[name] = value
    handler.rfile = io.BytesIO(data)
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    getattr(handler, "do_" + method)()
    return (handler.send_response.call_args.args[0], handler.wfile.getvalue().decode(),
            dict(call.args for call in handler.send_header.call_args_list))


def post(server, *, token=TOKEN, csrf=None, origin=None, host=None, data=None):
    if data is None:
        data = parse.urlencode({"token": token, "csrf": csrf or server.setup.csrf}).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Origin": origin or server.origin}
    if host:
        headers["Host"] = host
    status, body, _ = handle(server, "POST", data=data, headers=headers)
    return status, body


def test_getme_validates_expected_bot_without_echo(monkeypatch):
    recorded = []

    def fake_open(req, **kwargs):
        recorded.append((req, kwargs))
        return io.BytesIO(json.dumps({"ok": True, "result": {
            "is_bot": True, "username": "DOATAPEXAMBOT"}}).encode())

    monkeypatch.setattr(local_setup.request, "urlopen", fake_open)
    assert local_setup.validate_bot_token(TOKEN)
    req, kwargs = recorded[0]
    assert req.full_url == f"https://api.telegram.org/bot{TOKEN}/getMe"
    assert req.get_method() == "GET" and kwargs["timeout"] == 15
    assert kwargs["context"].check_hostname


@pytest.mark.parametrize("payload", [
    {"ok": False},
    {"ok": True, "result": {"is_bot": True, "username": "wrong_bot"}},
    {"ok": True, "result": {"is_bot": False, "username": "DOATAPEXAMBOT"}},
])
def test_getme_rejects_other_bot_and_api_failure(monkeypatch, payload):
    monkeypatch.setattr(local_setup.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    assert not local_setup.validate_bot_token(TOKEN)


def test_getme_network_error_does_not_escape_or_log(monkeypatch, capsys):
    def failure(*args, **kwargs):
        raise RuntimeError(f"request URL contains {TOKEN}")

    monkeypatch.setattr(local_setup.request, "urlopen", failure)
    assert not local_setup.validate_bot_token(TOKEN)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_secure_env_detached_process_and_duplicate_submission(setup, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "old_wrong_token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://wrong")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    success, message = setup.start(TOKEN)
    assert success and message == ""
    env = setup.root / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    contents = env.read_text()
    assert f"BOT_TOKEN={TOKEN}" in contents
    assert "DATABASE_URL=sqlite:///runtime/doatap.db" in contents
    assert str(setup.bank) in contents
    assert setup.launcher.call_count == 1
    args, kwargs = setup.launcher.call_args
    assert args[0] == [str(setup.root / ".venv/bin/python"), "-m", "doatap", "run"]
    assert kwargs["start_new_session"] is True and kwargs["cwd"] == setup.root
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    assert "BOT_TOKEN" not in kwargs["env"] and "DATABASE_URL" not in kwargs["env"]
    assert "PYTHON_DOTENV_DISABLED" not in kwargs["env"]
    assert setup.start(TOKEN)[0]
    assert setup.launcher.call_count == 1
    for name in ("bot.log", "bot.pid"):
        assert stat.S_IMODE((setup.root / "output/local" / name).stat().st_mode) == 0o600
    assert TOKEN not in (setup.root / "output/local/bot.log").read_text()


@pytest.mark.parametrize("contents", ["", "BOT_TOKEN=existing\n"])
def test_existing_env_is_never_overwritten(setup, contents):
    env = setup.root / ".env"
    env.write_text(contents)
    assert not setup.start(TOKEN)[0]
    assert env.read_text() == contents
    setup.validator.assert_not_called()
    setup.launcher.assert_not_called()


def test_failed_validation_does_not_save_or_launch(setup):
    setup.validator.return_value = False
    assert not setup.start(TOKEN)[0]
    assert not (setup.root / ".env").exists()
    setup.launcher.assert_not_called()


def test_http_form_is_private_and_success_never_echoes_token(server, capsys):
    assert server.server_address[0] == "127.0.0.1"
    status, body, headers = handle(server, "GET")
    assert "type=password" in body
    assert headers["Cache-Control"] == "no-store"
    # Same-origin keeps Origin valid on a browser form POST while suppressing
    # secret setup URLs when navigating to another origin.
    assert headers["Referrer-Policy"] == "same-origin"
    status, body = post(server)
    assert status == 303 and TOKEN not in body
    status, body, _ = handle(server, "GET", path=server.setup.path + "/status")
    assert "data-state='starting'" in body
    assert post(server)[0] == 303
    server.setup.launcher.assert_called_once()
    server.setup.process.poll.return_value = 1
    status, body, _ = handle(server, "GET", path=server.setup.path + "/status")
    assert "data-state='failed'" in body and TOKEN not in body
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_success_uses_post_redirect_get(server):
    data = parse.urlencode({"token": TOKEN, "csrf": server.setup.csrf}).encode()
    status, body, headers = handle(server, "POST", data=data, headers={
        "Origin": server.origin, "Content-Type": "application/x-www-form-urlencoded"})
    assert status == 303
    assert headers["Location"] == server.setup.path + "/status"
    assert TOKEN not in body and "http-equiv=\"refresh\"" not in body


@pytest.mark.parametrize("kwargs", [
    {"csrf": "bad-csrf"}, {"origin": "http://attacker.example"}, {"host": "attacker.example"},
])
def test_http_rejects_forged_origin_host_and_csrf(server, kwargs):
    status, body = post(server, **kwargs)
    assert status == 403 and TOKEN not in body
    server.setup.validator.assert_not_called()
    server.setup.launcher.assert_not_called()


def test_oversized_request_and_invalid_token_do_not_echo(server):
    assert post(server, data=b"x" * (local_setup.MAX_REQUEST_BYTES + 1))[0] == 413
    status, body = post(server, token="secret-invalid-<token>")
    assert status == 400 and "secret-invalid" not in body
    assert not (server.setup.root / ".env").exists()


def test_second_helper_instance_cannot_start_duplicate(setup):
    assert setup.start(TOKEN)[0]
    second_launcher = Mock()
    second = local_setup.LocalSetup(setup.root, setup.bank, validator=Mock(return_value=True),
                                   launcher=second_launcher)
    assert not second.start(TOKEN)[0]
    second_launcher.assert_not_called()
