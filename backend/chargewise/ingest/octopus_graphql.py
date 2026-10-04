"""Octopus Energy GraphQL adapter — Intelligent Octopus Go smart-charge dispatches.

The dispatch feed is what makes daytime off-peak charging cost correctly: a slot
covered by a completed dispatch is billed at the off-peak rate even outside the
core window. Shape mirrors the Octopus GraphQL `completedDispatches` /
`plannedDispatches` (and the BottlecapDave HA integration attributes):
    { "start": ISO, "end": ISO, "charge_in_kwh": float, "source": str, "location": str }
charge_in_kwh is negative while charging.

GraphQL endpoint: https://api.octopus.energy/v1/graphql

The endpoint reports an auth or query failure as HTTP 200 with ``data: null``
and an ``errors`` array, so every response goes through ``graphql_field``,
which turns that into an ``OctopusGraphQLError`` naming what Octopus said.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..engine.models import Dispatch

GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

#: Longest error detail kept from a GraphQL response.
MAX_DETAIL_CHARS = 200

# Wording Octopus uses when it refuses the credentials (e.g. "Authentication
# failed.", "Unauthorized.", "Signature of the JWT has expired.").
_AUTH_WORDS = re.compile(r"(?i)authenticat|unauthori[sz]ed|not authori[sz]ed|\bjwt\b")

COMPLETED_DISPATCHES_QUERY = """
query Dispatches($accountNumber: String!) {
  completedDispatches(accountNumber: $accountNumber) {
    start
    end
    deltaKwh
    meta { source location }
  }
}
"""


class OctopusGraphQLError(RuntimeError):
    """The GraphQL endpoint answered with errors, or without the data asked for.

    ``auth_failed`` is True when the errors say the credentials were refused.
    """

    def __init__(self, operation: str, detail: str, auth_failed: bool = False) -> None:
        super().__init__(f"Octopus GraphQL {operation} failed: {detail}")
        self.operation = operation
        self.auth_failed = auth_failed


def _error_texts(errors: Any) -> list[str]:
    """One line per GraphQL error: its message plus Octopus's code/description."""
    texts: list[str] = []
    for err in errors if isinstance(errors, list) else []:
        if not isinstance(err, dict):
            continue
        message = str(err.get("message") or "").strip()
        ext = err.get("extensions")
        extra = ""
        if isinstance(ext, dict):
            extra = " ".join(
                str(ext[key]).strip() for key in ("errorCode", "errorDescription") if ext.get(key)
            )
        text = f"{message} [{extra}]" if message and extra else message or extra
        if text:
            texts.append(text)
    return texts


def graphql_field(payload: Any, field: str, secrets: tuple[str, ...] = ()) -> Any:
    """Return ``payload["data"][field]``, or raise ``OctopusGraphQLError``.

    Raises when the response carries ``errors`` or no value for ``field``. The
    exception quotes the first few error messages, truncated, with ``secrets``
    (the API key, the session token) masked.
    """
    errors = payload.get("errors") if isinstance(payload, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    value = data.get(field) if isinstance(data, dict) else None
    if not errors and value is not None:
        return value

    texts = _error_texts(errors)
    for secret in secrets:
        if secret:
            texts = [text.replace(secret, "***") for text in texts]
    detail = "; ".join(texts[:3]) or "no data returned"
    if len(detail) > MAX_DETAIL_CHARS:
        detail = detail[: MAX_DETAIL_CHARS - 3] + "..."
    auth_failed = any(_AUTH_WORDS.search(text) for text in texts)
    raise OctopusGraphQLError(field, detail, auth_failed)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_dispatches(items: list[dict]) -> list[Dispatch]:
    """Map raw dispatch dicts (GraphQL or HA-attribute shape) to Dispatch models."""
    out: list[Dispatch] = []
    for it in items:
        location = it.get("location") or it.get("meta", {}).get("location", "unknown")
        out.append(Dispatch(_dt(it["start"]), _dt(it["end"]), location or "unknown"))
    return out


class OctopusGraphQLClient:
    """Thin network wrapper for the dispatch feed. Parsing is pure above."""

    def __init__(self, api_key: str, url: str = GRAPHQL_URL) -> None:
        self.api_key = api_key
        self.url = url

    async def get_completed_dispatches(self, account_number: str) -> list[Dispatch]:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._obtain_token(client)
            resp = await client.post(
                self.url,
                json={
                    "query": COMPLETED_DISPATCHES_QUERY,
                    "variables": {"accountNumber": account_number},
                },
                headers={"Authorization": token},
            )
            resp.raise_for_status()
            data = graphql_field(resp.json(), "completedDispatches", (self.api_key, token))
            if not isinstance(data, list):
                raise OctopusGraphQLError("completedDispatches", "unexpected response shape")
            normalised = [
                {"start": d["start"], "end": d["end"],
                 "location": (d.get("meta") or {}).get("location", "unknown")}
                for d in data
            ]
            return parse_dispatches(normalised)

    async def _obtain_token(self, client) -> str:
        mutation = (
            "mutation($apiKey: String!) "
            "{ obtainKrakenToken(input: {APIKey: $apiKey}) { token } }"
        )
        resp = await client.post(
            self.url, json={"query": mutation, "variables": {"apiKey": self.api_key}}
        )
        resp.raise_for_status()
        granted = graphql_field(resp.json(), "obtainKrakenToken", (self.api_key,))
        token = granted.get("token") if isinstance(granted, dict) else None
        if not isinstance(token, str) or not token:
            raise OctopusGraphQLError("obtainKrakenToken", "no token in the response")
        return token
