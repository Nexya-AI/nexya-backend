"""
Feature Notifications — F3.

Dispatcher dual-channel (push FCM + email fallback), préférences par
catégorie × canal, timeline in-app persistée, lien unsubscribe one-click
RGPD/CAN-SPAM.

Architecture :

    worker Planner / futurs émetteurs
              ↓
    NotificationDispatcher.dispatch(user, category, title, body, data)
              ↓
    ┌─────────┴──────────┐
    │                    │
    ▼                    ▼
    try_push(FCM)      try_email(Brevo/Mock)
    │                    │
    └────────┬───────────┘
             ▼
    INSERT notifications row (channel_used réel)
             ▼
    GET /notifications → timeline user

Préférences :
- `GET /user/notification-preferences`
- `PUT /user/notification-preferences`

Unsubscribe public :
- `POST /notifications/unsubscribe/{token}` (RGPD one-click, JWT 365j)
"""

from __future__ import annotations

from .models import Notification, NotificationPreference

__all__ = ["Notification", "NotificationPreference"]
