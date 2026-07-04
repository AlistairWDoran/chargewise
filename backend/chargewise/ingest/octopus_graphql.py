"""Octopus Energy GraphQL adapter — Intelligent Octopus Go smart-charge dispatches.

The dispatch feed is what makes daytime off-peak charging cost correctly: a slot
covered by a completed dispatch is billed at the off-peak rate even outside the
core window. Shape mirrors the Octopus GraphQL `completedDispatches` /
`plannedDispatches` (and the BottlecapDave HA integration attributes):
    { "start": ISO, "end": ISO, "charge_in_kwh": float, "source": str, "location": str }
charge_in_kwh is negative while charging.

GraphQL endpoint: https://api.octopus.energy/v1/graphql
"""

from __future__ import annotations

from datetime import datetime

from ..engine.models import Dispatch

GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

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
            data = resp.json()["data"]["completedDispatches"]
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
        return resp.json()["data"]["obtainKrakenToken"]["token"]
