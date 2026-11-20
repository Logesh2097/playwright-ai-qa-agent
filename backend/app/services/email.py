"""Outbound email: invitations and password-reset links.

Services depend on the `EmailSender` protocol. With `SMTP_HOST` set, messages go
out through that server (`SmtpEmailSender`). Without it, the message is written
to the log in development and test; in production its body is never logged,
because it contains a single-use link, and the missing transport is an error.
"""

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from typing import Protocol

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class LogEmailSender:
    """Development stand-in for a mail transport."""

    def __init__(self, settings: Settings) -> None:
        self._log_bodies = not settings.is_production

    async def send(self, message: EmailMessage) -> None:
        if self._log_bodies:
            # `email_body` is intentionally not named like a secret: in development the
            # link in it is the only way to complete the flow.
            logger.info(
                "email_not_delivered_dev_mode",
                extra={"to": message.to, "subject": message.subject, "email_body": message.body},
            )
        else:
            logger.error(
                "email_not_delivered_no_transport",
                extra={"to": message.to, "subject": message.subject},
            )


def password_reset_email(*, to: str, base_url: str, token: str, ttl_minutes: int) -> EmailMessage:
    link = f"{base_url.rstrip('/')}/reset-password?token={token}"
    return EmailMessage(
        to=to,
        subject="Reset your QA Agent password",
        body=(
            "Someone asked to reset the password for this account.\n\n"
            f"Open this link within {ttl_minutes} minutes to choose a new password:\n{link}\n\n"
            "If this wasn't you, you can ignore this email; your password has not changed."
        ),
    )


def invitation_email(
    *, to: str, base_url: str, token: str, organization_name: str, inviter_name: str
) -> EmailMessage:
    link = f"{base_url.rstrip('/')}/accept-invite?token={token}"
    return EmailMessage(
        to=to,
        subject=f"You're invited to {organization_name} on QA Agent",
        body=(
            f"{inviter_name} invited you to join {organization_name}.\n\n"
            f"Accept the invitation here:\n{link}"
        ),
    )


class SmtpEmailSender:
    """Sends through an SMTP server, over TLS. Plain text only.

    A message that cannot be sent is logged without its body and not raised: the
    flows that send mail answer the same whether or not an address exists, and
    must not start failing visibly when a mail server is down.
    """

    def __init__(self, settings: Settings) -> None:
        self._host = str(settings.smtp_host)
        self._port = settings.smtp_port
        self._username = settings.smtp_username
        self._password = (
            settings.smtp_password.get_secret_value() if settings.smtp_password else None
        )
        self._sender = settings.smtp_from or f"no-reply@{self._host}"
        self._implicit_tls = settings.smtp_port == 465
        self._timeout = 15

    def _deliver(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"], mime["To"], mime["Subject"] = self._sender, message.to, message.subject
        mime.set_content(message.body)
        context = ssl.create_default_context()
        if self._implicit_tls:
            with smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=context
            ) as server:
                self._send(server, mime)
        else:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
                # Never send a link or a password over a connection that is not encrypted.
                server.starttls(context=context)
                self._send(server, mime)

    def _send(self, server: smtplib.SMTP, mime: MimeMessage) -> None:
        if self._username and self._password:
            server.login(self._username, self._password)
        server.send_message(mime)

    async def send(self, message: EmailMessage) -> None:
        try:
            await asyncio.to_thread(self._deliver, message)
        except (OSError, smtplib.SMTPException) as exc:
            logger.error(
                "email_not_delivered",
                extra={"to": message.to, "subject": message.subject, "reason": type(exc).__name__},
            )
            return
        logger.info("email_sent", extra={"to": message.to, "subject": message.subject})


def build_email_sender(settings: Settings) -> EmailSender:
    return SmtpEmailSender(settings) if settings.smtp_host else LogEmailSender(settings)
