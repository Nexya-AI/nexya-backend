"""Package email — API publique du module."""

from app.core.email.base import EmailMessage, EmailSendException, EmailService
from app.core.email.factory import (
    close_email_service,
    get_email_service,
    reset_email_service_for_tests,
)
from app.core.email.renderer import TemplateRenderer, get_template_renderer

__all__ = [
    "EmailMessage",
    "EmailSendException",
    "EmailService",
    "TemplateRenderer",
    "close_email_service",
    "get_email_service",
    "get_template_renderer",
    "reset_email_service_for_tests",
]
