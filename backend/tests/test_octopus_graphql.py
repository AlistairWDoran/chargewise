"""Octopus GraphQL client: errors arrive as HTTP 200 and must be named, not crash.

On an auth or query failure the endpoint answers ``200`` with ``data: null``
and an ``errors`` array. That used to surface as ``TypeError: 'NoneType' object
is not subscriptable``. All HTTP is served by a mocked transport.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from chargewise.engine.models import Dispatch
from chargewise.ingest import pipeline
from chargewise.ingest.octopus_graphql import (
    MAX_DETAIL_CHARS,
    OctopusGraphQLClient,
    OctopusGraphQLError,
    graphql_field,
)

REAL_ASYNC_CLIENT = httpx.AsyncClient
API_KEY = "sk_test_not-a-real-key"
JWT = "jwt.not-a-real.token"
ACCOUNT = "A-00000000"

TOKEN_OK = {"data": {"obtainKrakenToken": {"token": JWT}}}
DISPATCHES_OK = {"data": {"completedDispatches": [
    {"start": "2026-06-15T12:00:00+00:00", "end": "2026-06-15T12:30:00+00:00",
     "deltaKwh": "-3.5", "meta": {"source": "smart-charge", "location": "AT_HOME"}},
    {"start": "2026-06-16T10:00:00Z", "end": "2026-06-16T11:00:00Z",
     "deltaKwh": "-7.0", "meta": None},
]}}
# The shape Octopus really sends when it refuses an API key.
TOKEN_REFUSED = {
    "errors": [{
        "message": "Invalid data.",
        "locations": [{"line": 1, "column": 29}],
        "path": ["obtainKrakenToken"],
        "extensions": {
            "errorType": "VALIDATION", "errorCode": "KT-CT-1139",
            "errorDescription": "Authentication failed.",
        },
    }],
    "data": {"obtainKrakenToken": None},
}


def mock_graphql(monkeypatch, token_reply: dict, dispatch_reply: dict | None = None):
    """Serve the token mutation and the dispatch query; returns the request bodies seen."""
    seen: list[tuple[dict, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body, request.headers.get("Authorization")))
        if "obtainKrakenToken" in body["query"]:
            return httpx.Response(200, json=token_reply)
        assert dispatch_reply is not None, "the dispatch query should not have been sent"
        return httpx.Response(200, json=dispatch_reply)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )
    return seen


def fetch() -> list[Dispatch]:
    return asyncio.run(OctopusGraphQLClient(API_KEY).get_completed_dispatches(ACCOUNT))


def test_success_returns_dispatches_and_sends_the_token(monkeypatch):
    seen = mock_graphql(monkeypatch, TOKEN_OK, DISPATCHES_OK)
    assert fetch() == [
        Dispatch(datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
                 datetime(2026, 6, 15, 12, 30, tzinfo=timezone.utc), "AT_HOME"),
        Dispatch(datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
                 datetime(2026, 6, 16, 11, 0, tzinfo=timezone.utc), "unknown"),
    ]
    (token_body, token_auth), (query_body, query_auth) = seen
    assert token_body["variables"] == {"apiKey": API_KEY} and token_auth is None
    assert query_body["variables"] == {"accountNumber": ACCOUNT} and query_auth == JWT


def test_no_dispatches_is_an_empty_list_not_an_error(monkeypatch):
    mock_graphql(monkeypatch, TOKEN_OK, {"data": {"completedDispatches": []}})
    assert fetch() == []


def test_errors_with_null_data_on_the_token_call(monkeypatch):
    seen = mock_graphql(monkeypatch, TOKEN_REFUSED)
    with pytest.raises(OctopusGraphQLError) as caught:
        fetch()
    assert str(caught.value) == (
        "Octopus GraphQL obtainKrakenToken failed: "
        "Invalid data. [KT-CT-1139 Authentication failed.]"
    )
    assert caught.value.operation == "obtainKrakenToken"
    assert caught.value.auth_failed is True
    assert len(seen) == 1   # no dispatch query without a token
    assert pipeline.describe_error(caught.value, "octopus") == (
        "Octopus rejected the API key (GraphQL: authentication failed) - check OCTOPUS_API_KEY"
    )


def test_errors_with_null_data_on_the_dispatch_query(monkeypatch):
    reply = {
        "errors": [
            {"message": f"No smart device for {ACCOUNT} (token {JWT}, key {API_KEY})."},
            {"message": "Second problem."},
        ],
        "data": None,
    }
    mock_graphql(monkeypatch, TOKEN_OK, reply)
    with pytest.raises(OctopusGraphQLError) as caught:
        fetch()
    assert str(caught.value) == (
        f"Octopus GraphQL completedDispatches failed: "
        f"No smart device for {ACCOUNT} (token ***, key ***).; Second problem."
    )
    assert caught.value.operation == "completedDispatches"
    assert caught.value.auth_failed is False
    # Not an auth failure, so the status API gets the message itself (account masked).
    assert pipeline.describe_error(caught.value, "octopus", (API_KEY, ACCOUNT)) == (
        "OctopusGraphQLError: Octopus GraphQL completedDispatches failed: "
        "No smart device for *** (token ***, key ***).; Second problem."
    )


@pytest.mark.parametrize("message", [
    "Signature of the JWT has expired.", "Unauthorized.", "You are not authorised to view this.",
])
def test_auth_failure_on_the_dispatch_query_is_recognised(monkeypatch, message):
    mock_graphql(monkeypatch, TOKEN_OK,
                 {"errors": [{"message": message}], "data": {"completedDispatches": None}})
    with pytest.raises(OctopusGraphQLError) as caught:
        fetch()
    assert caught.value.auth_failed is True
    assert pipeline.describe_error(caught.value, "octopus").startswith(
        "Octopus rejected the API key"
    )
    # A wrapping exception does not hide it; another source never gets this wording.
    try:
        raise RuntimeError("octopus step failed") from caught.value
    except RuntimeError as wrapped:
        assert pipeline.describe_error(wrapped, "octopus").startswith(
            "Octopus rejected the API key"
        )
        assert pipeline.describe_error(wrapped, "teslafi") == "RuntimeError: octopus step failed"


@pytest.mark.parametrize("reply, detail", [
    ({"data": None}, "no data returned"),
    ({"data": {"completedDispatches": None}}, "no data returned"),
    ({"data": {"completedDispatches": {"not": "a list"}}}, "unexpected response shape"),
    ({"errors": [{"message": ""}, "junk"], "data": None}, "no data returned"),
    # Errors alongside data: the list may be incomplete, so it is not trusted.
    ({"errors": [{"message": "Partial failure."}], "data": {"completedDispatches": []}},
     "Partial failure."),
])
def test_missing_or_malformed_dispatch_data_is_a_named_error(monkeypatch, reply, detail):
    mock_graphql(monkeypatch, TOKEN_OK, reply)
    with pytest.raises(OctopusGraphQLError, match=f"completedDispatches failed: {detail}"):
        fetch()


@pytest.mark.parametrize("reply", [
    {"data": {"obtainKrakenToken": {"token": None}}},
    {"data": {"obtainKrakenToken": {"token": ""}}},
    {"data": {"obtainKrakenToken": "not-an-object"}},
])
def test_token_reply_without_a_token_is_a_named_error(monkeypatch, reply):
    mock_graphql(monkeypatch, reply)
    with pytest.raises(OctopusGraphQLError, match="obtainKrakenToken failed: no token"):
        fetch()


def test_error_detail_is_truncated_and_limited_to_three_errors():
    errors = [{"message": f"problem {n} " + "x" * 90} for n in range(1, 6)]
    with pytest.raises(OctopusGraphQLError) as caught:
        graphql_field({"errors": errors, "data": None}, "completedDispatches")
    detail = str(caught.value).split("failed: ", 1)[1]
    assert len(detail) == MAX_DETAIL_CHARS and detail.endswith("...")
    assert "problem 1" in detail and "problem 4" not in detail

    short = [{"message": f"problem {n}"} for n in range(1, 6)]
    with pytest.raises(OctopusGraphQLError, match="failed: problem 1; problem 2; problem 3$"):
        graphql_field({"errors": short, "data": None}, "completedDispatches")


def test_graphql_field_returns_the_value_when_there_are_no_errors():
    assert graphql_field({"data": {"x": []}}, "x") == []
    assert graphql_field({"data": {"x": {"token": "t"}}, "errors": []}, "x") == {"token": "t"}
    with pytest.raises(OctopusGraphQLError, match="x failed: no data returned"):
        graphql_field(["not", "an", "object"], "x")
