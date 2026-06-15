import pytest

from app.services.email_sender import EmailDeliveryError, load_smtp_settings_from_env, send_email


class FakeSMTP:
    def __init__(self, *args, **kwargs):
        self.started_tls = False
        self.logged_in = False
        self.sent = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.logged_in = True

    def send_message(self, message):
        self.sent = True


class BrokenSMTP(FakeSMTP):
    def login(self, username, password):
        raise RuntimeError("invalid credentials")


def _set_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "pass")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.com")
    monkeypatch.setenv("SMTP_USE_TLS", "true")


def test_load_smtp_settings_from_env(monkeypatch):
    _set_env(monkeypatch)
    settings = load_smtp_settings_from_env()
    assert settings.host == "smtp.example.com"
    assert settings.port == 587
    assert settings.use_tls is True


def test_send_email_success(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setattr("app.services.email_sender.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("app.services.email_sender.smtplib.SMTP_SSL", FakeSMTP)
    send_email("to@example.com", "Subject", "Body")


def test_send_email_failure(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setattr("app.services.email_sender.smtplib.SMTP", BrokenSMTP)
    monkeypatch.setattr("app.services.email_sender.smtplib.SMTP_SSL", BrokenSMTP)
    with pytest.raises(EmailDeliveryError):
        send_email("to@example.com", "Subject", "Body")
