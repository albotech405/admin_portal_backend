import json
import logging
from typing import Dict, List, Optional

import firebase_admin
from firebase_admin import credentials, messaging

from app.core.config import settings

logger = logging.getLogger(__name__)

_app: Optional[firebase_admin.App] = None


def _get_app() -> firebase_admin.App:
    """Initialize the Firebase app once and return it."""
    global _app
    if _app is not None:
        return _app

    raw = settings.FIREBASE_SERVICE_ACCOUNT_JSON.strip()
    if not raw or raw == "{}":
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not configured. "
            "Set it in your .env to enable push notifications."
        )

    service_account_info = json.loads(raw)
    cred = credentials.Certificate(service_account_info)
    _app = firebase_admin.initialize_app(cred)
    return _app


def send_push_notification(
    token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> bool:
    """
    Send a push notification to a single device token.
    Returns True on success, False on failure.
    """
    try:
        _get_app()
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            token=token,
        )
        messaging.send(message)
        return True
    except Exception as exc:
        logger.warning("FCM send failed for token %s…: %s", token[:10], exc)
        return False


def send_push_multicast(
    tokens: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
) -> Dict[str, int]:
    """
    Send a push notification to multiple device tokens.
    FCM supports up to 500 tokens per request; this function chunks automatically.
    Returns {"success": n, "failure": m}.
    """
    if not tokens:
        return {"success": 0, "failure": 0}

    try:
        _get_app()
        success_count = 0
        failure_count = 0
        chunk_size = 500

        for i in range(0, len(tokens), chunk_size):
            chunk = tokens[i : i + chunk_size]
            message = messaging.MulticastMessage(
                notification=messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in (data or {}).items()},
                tokens=chunk,
            )
            response = messaging.send_each_for_multicast(message)
            success_count += response.success_count
            failure_count += response.failure_count

        return {"success": success_count, "failure": failure_count}

    except Exception as exc:
        logger.error("FCM multicast failed: %s", exc)
        return {"success": 0, "failure": len(tokens)}
