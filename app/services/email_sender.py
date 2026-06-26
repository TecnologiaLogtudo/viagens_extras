from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    pass


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    username: str
    password: str
    from_email: str
    use_tls: bool = True
    use_ssl: bool = False


REQUIRED_ENV_VARS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM_EMAIL",
)


def _parse_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_smtp_settings_from_env() -> SMTPSettings:
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise EmailDeliveryError(
            "Configuração SMTP ausente. Defina: " + ", ".join(missing)
        )

    try:
        port = int((os.getenv("SMTP_PORT", "0") or "").strip())
    except ValueError as exc:
        raise EmailDeliveryError("SMTP_PORT inválida. Use um número inteiro.") from exc

    return SMTPSettings(
        host=os.getenv("SMTP_HOST", "").strip(),
        port=port,
        username=os.getenv("SMTP_USERNAME", "").strip(),
        password=os.getenv("SMTP_PASSWORD", ""),
        from_email=os.getenv("SMTP_FROM_EMAIL", "").strip(),
        use_tls=_parse_bool(os.getenv("SMTP_USE_TLS"), default=True),
        use_ssl=_parse_bool(os.getenv("SMTP_USE_SSL"), default=False),
    )


def send_email(
    to_email: str,
    subject: str,
    body: str,
    timeout_seconds: int = 10,
    attachment_path: str | None = None,
) -> None:
    # Ignora o envio para domínios de teste local (.local) para evitar erros de SMTP
    if to_email.strip().lower().endswith(".local"):
        logger.info("Envio de e-mail ignorado para destinatário mock: %s", to_email)
        return

    settings = load_smtp_settings_from_env()

    message = EmailMessage()
    message["From"] = settings.from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    if attachment_path and os.path.exists(attachment_path):
        import mimetypes
        ctype, encoding = mimetypes.guess_type(attachment_path)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(attachment_path, "rb") as fp:
            file_data = fp.read()
        message.add_attachment(
            file_data,
            maintype=maintype,
            subtype=subtype,
            filename=os.path.basename(attachment_path),
        )

    try:
        use_ssl = settings.use_ssl or settings.port == 465
        if use_ssl:
            with smtplib.SMTP_SSL(settings.host, settings.port, timeout=timeout_seconds) as smtp:
                smtp.login(settings.username, settings.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.host, settings.port, timeout=timeout_seconds) as smtp:
                if settings.use_tls:
                    smtp.starttls()
                smtp.login(settings.username, settings.password)
                smtp.send_message(message)
    except Exception as exc:
        raise EmailDeliveryError(f"Falha ao enviar e-mail via SMTP: {exc}") from exc
