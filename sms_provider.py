"""Provider-independent SMS adapter for ScantyMeals.

Gonline is the production SMS provider. Its contract is:

    GET https://sms.gonlinesites.com/app/sms/api
        action  = "send-sms"
        api_key = GONLINE_API_KEY          (env)
        to      = international number, e.g. +233244000000
        from    = GONLINE_SENDER_ID        (registered sender ID)
        sms     = message body
    Success:  {"code": "OK", "message_id": ..., "to": ..., "units": ..., "balance": ...}
    Errors:   numeric code (100..111) => permanent (non-retryable)

All credentials come from environment variables; never hardcode secrets, and
never log the API key, full request URL, or credentials.
"""

import os
import uuid

import requests


class BaseSmsProvider:
    name = "base"

    def send(self, phone, message):
        raise NotImplementedError


class MockSmsProvider(BaseSmsProvider):
    """Simulates a successful send for development/testing. NEVER real delivery."""

    name = "mock"

    def send(self, phone, message):
        return True, {
            "provider": "mock",
            "message_id": "mock-" + uuid.uuid4().hex,
            "simulated": True,
            "note": "Simulated SMS — no real message was delivered",
        }


class GonlineSmsProvider(BaseSmsProvider):
    """Real Gonline SMS integration using the official API contract."""

    name = "gonline"

    def __init__(self):
        self.api_url = os.getenv("GONLINE_API_URL", "https://sms.gonlinesites.com/app/sms/api")
        self.api_key = os.getenv("GONLINE_API_KEY")
        self.sender_id = os.getenv("GONLINE_SENDER_ID", "SCANTYMEALS")
        self.timeout = 30
        self.ready = bool(self.api_url and self.api_key)

    def send(self, phone, message):
        if not self.ready:
            return False, {
                "provider": "gonline",
                "status": "not_configured",
                "error": "Gonline API URL/KEY not configured",
                "retryable": False,
            }

        params = {
            "action": "send-sms",
            "api_key": self.api_key,
            "to": phone,
            "from": self.sender_id,
            "sms": message,
        }

        try:
            resp = requests.get(self.api_url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout:
            return False, {"provider": "gonline", "error": "timeout", "retryable": True}
        except requests.exceptions.RequestException:
            return False, {"provider": "gonline", "error": "network_error", "retryable": True}

        try:
            data = resp.json()
        except (ValueError, TypeError):
            return False, {"provider": "gonline", "error": "invalid_response",
                           "http_status": resp.status_code, "retryable": True}

        code = data.get("code")
        if code == "OK":
            # Accepted by the provider. This is NOT confirmed delivery.
            return True, {
                "provider": "gonline",
                "code": "OK",
                "message_id": data.get("message_id"),
                "to": data.get("to"),
                "units": data.get("units"),
                "balance": data.get("balance"),
            }

        # Numeric Gonline error codes are permanent: do not auto-retry.
        return False, {
            "provider": "gonline",
            "code": code,
            "error": f"Gonline error {code}",
            "retryable": False,
        }


def get_sms_provider():
    """Return the configured SMS provider (mock by default)."""
    name = (os.getenv("SMS_PROVIDER") or "mock").strip().lower()
    if name == "gonline":
        return GonlineSmsProvider()
    return MockSmsProvider()
