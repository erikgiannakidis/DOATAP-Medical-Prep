from pathlib import Path

import pytest

from doatap.config import Settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    for key in ('BOT_TOKEN', 'DATABASE_URL', 'QUESTION_BANK_PATH', 'ALLOWED_USER_IDS'):
        monkeypatch.delenv(key, raising=False)


def test_run_requires_token_but_import_does_not(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///test.db')
    assert Settings.from_env(require_token=False).token is None
    with pytest.raises(ValueError, match='BOT_TOKEN'):
        Settings.from_env()


def test_missing_database_is_not_silently_ephemeral():
    with pytest.raises(ValueError, match='DATABASE_URL'):
        Settings.from_env(require_token=False)


def test_allowlist_and_bank(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///test.db')
    monkeypatch.setenv('ALLOWED_USER_IDS', '123, 456')
    monkeypatch.setenv('QUESTION_BANK_PATH', 'example/bank.jsonl')
    settings = Settings.from_env(require_token=False)
    assert settings.allowed_user_ids == frozenset({123, 456})
    assert settings.bank_path == Path('example/bank.jsonl')


@pytest.mark.parametrize('value', ['someone', '0', '-123'])
def test_bad_allowlist_is_rejected(monkeypatch, value):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///test.db')
    monkeypatch.setenv('ALLOWED_USER_IDS', value)
    with pytest.raises(ValueError, match='ALLOWED_USER_IDS'):
        Settings.from_env(require_token=False)


def test_secret_never_appears_in_configuration_errors(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///test.db')
    secret = 'not_a_real_secret_but_should_not_be_printed'
    monkeypatch.setenv('BOT_TOKEN', secret)
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert secret not in str(error.value)
