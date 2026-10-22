from __future__ import annotations

from typing import Tuple

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache


MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15
LOCK_SECONDS = LOCK_MINUTES * 60
LOCKED_MESSAGE = (
    "Too many failed login attempts for this username from your network. "
    "Please try again in 15 minutes."
)


def _client_ip(request) -> str:
    # Prefer the first address in X-Forwarded-For when present.
    forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip() or "unknown"


def _cache_keys(ip: str, username: str) -> Tuple[str, str]:
    safe_user = (username or "").strip().casefold()
    safe_ip = (ip or "").strip()
    return (
        f"login_fail:{safe_ip}:{safe_user}",
        f"login_lock:{safe_ip}:{safe_user}",
    )


class LimitedAuthenticationForm(AuthenticationForm):
    """
    Authentication form that enforces a small in-project rate limiter:
    - Count failed attempts by (IP, username)
    - Lock that pair for 15 minutes after 5 failures
    - A successful login clears the counter/lock
    """

    def clean(self):
        # Resolve identity for rate keying before attempting authentication.
        ip = _client_ip(self.request)
        # `data` carries the raw submitted value; normalize to case-insensitive.
        submitted_username = (self.data.get(self.username_field, "") or "").strip()
        fail_key, lock_key = _cache_keys(ip, submitted_username)

        # Refuse immediately while locked, even if the password is now correct.
        if cache.get(lock_key):
            raise forms.ValidationError(LOCKED_MESSAGE)

        try:
            cleaned = super().clean()
        except forms.ValidationError as e:
            # Increment the failure counter with a TTL. On threshold, set a lock.
            failures = int(cache.get(fail_key) or 0) + 1
            cache.set(fail_key, failures, LOCK_SECONDS)
            if failures >= MAX_FAILED_ATTEMPTS:
                cache.set(lock_key, 1, LOCK_SECONDS)
                raise forms.ValidationError(LOCKED_MESSAGE)
            # Not locked yet: surface the standard invalid-login message.
            raise e

        # Successful authentication: clear any recorded failures/lock.
        cache.delete_many([fail_key, lock_key])
        return cleaned

