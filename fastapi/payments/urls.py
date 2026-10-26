"""
CheriPic-only redirect targets for checkout success/cancel and portal return.

Same rule as main._safe_return_url (replicated here so the payments package
never imports main — main imports the payments router at startup): the app
web origin (APP_BASE_URL), a FRONTEND_URLS origin, or the app's own
cheripic:// scheme. Anything else becomes the default path on APP_BASE_URL.
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlsplit

_DEFAULT_FRONTEND_URLS = (
    "http://localhost:5173,http://localhost:5174,http://localhost:5175,"
    "http://localhost:5176,http://127.0.0.1:5173"
)


def app_base_url() -> str:
    return (os.getenv("APP_BASE_URL") or "https://cheripic.com").rstrip("/")


def safe_return_url(url: Optional[str], default_path: str = "/#/profile") -> str:
    base = app_base_url()
    frontend = (os.getenv("FRONTEND_URLS") or _DEFAULT_FRONTEND_URLS).split(",")
    allowed = {urlsplit(base).netloc} | {urlsplit(u.strip()).netloc for u in frontend if u.strip()}
    try:
        parts = urlsplit(url or "")
    except ValueError:
        parts = None
    if parts and parts.scheme == "cheripic":
        return url  # type: ignore[return-value]
    if parts and parts.scheme in ("https", "http") and parts.netloc in allowed and not parts.username:
        return url  # type: ignore[return-value]
    return base + default_path


def default_return_urls(platform: str) -> tuple:
    """(success_url, cancel_url) when the client didn't send any."""
    if platform in ("ios", "android"):
        return "cheripic://billing/success", "cheripic://billing/cancel"
    base = app_base_url()
    return base + "/#/profile?checkout=success", base + "/#/pricing"
