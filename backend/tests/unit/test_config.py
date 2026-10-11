import pytest
from app.config import Settings


def test_env_defaults_to_prod_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails closed: dev endpoints and unsigned webhooks need an explicit CASELOOP_ENV=dev."""
    monkeypatch.delenv("CASELOOP_ENV", raising=False)
    settings = Settings(_env_file=None)
    assert settings.env == "prod" and not settings.is_dev
    monkeypatch.setenv("CASELOOP_ENV", "dev")
    assert Settings(_env_file=None).is_dev
