"""Tests for graph_api.GraphClient: tokens, retries, errors, and paging.

No network: the client's requests.Session is replaced by FakeSession, which
hands back scripted responses in order and records every request made.
time.sleep is replaced too, so the retry tests run instantly and can assert
exactly how long the client *would* have waited.

Run from the repo root:  python -m pytest -q
"""

import pytest

import graph_api
from graph_api import GRAPH_BASE, ConfigError, GraphClient, GraphError


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else ("" if payload is None else str(payload))

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def token_response(token="tok-1", expires_in=3600):
    return FakeResponse(200, {"access_token": token, "expires_in": expires_in})


class FakeSession:
    """Scripted stand-in for requests.Session.

      token_responses   answers for POSTs to the token endpoint, in order
      responses         answers for Graph requests, in order
      .token_posts      how many times a token was requested
      .requests         every Graph request as (method, url, kwargs)
    """

    def __init__(self, responses=(), token_responses=None):
        self.responses = list(responses)
        self.token_responses = list(token_responses or [token_response()])
        self.token_posts = 0
        self.requests = []

    def post(self, url, data=None, timeout=None):
        self.token_posts += 1
        self.last_token_request = (url, data)
        return self.token_responses.pop(0)

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method, url, dict(headers or {}), kwargs))
        return self.responses.pop(0)


@pytest.fixture
def sleeps(monkeypatch):
    """Record time.sleep calls instead of waiting."""
    recorded = []
    monkeypatch.setattr(graph_api.time, "sleep", recorded.append)
    return recorded


def make_client(responses=(), token_responses=None):
    client = GraphClient("tenant-id", "client-id", "client-secret")
    client.session = FakeSession(responses, token_responses)
    return client


# --- authentication -----------------------------------------------------------

def test_token_uses_the_client_credentials_flow():
    client = make_client([FakeResponse(200, {"value": []})])

    client.list_skus()

    url, data = client.session.last_token_request
    assert url == "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
    assert data["grant_type"] == "client_credentials"
    assert data["scope"] == "https://graph.microsoft.com/.default"
    assert client.session.requests[0][2]["Authorization"] == "Bearer tok-1"


def test_token_is_cached_across_requests():
    client = make_client([FakeResponse(200, {"value": []}), FakeResponse(200, {"value": []})])

    client.list_skus()
    client.list_skus()

    assert client.session.token_posts == 1


def test_expired_token_is_refreshed(monkeypatch):
    client = make_client(
        [FakeResponse(200, {"value": []}), FakeResponse(200, {"value": []})],
        token_responses=[token_response("tok-1"), token_response("tok-2")],
    )
    client.list_skus()

    client._token_expires = 0.0   # as if the hour had passed
    client.list_skus()

    assert client.session.token_posts == 2
    assert client.session.requests[1][2]["Authorization"] == "Bearer tok-2"


def test_token_is_refreshed_a_minute_early(monkeypatch):
    monkeypatch.setattr(graph_api.time, "time", lambda: 1000.0)
    client = make_client(token_responses=[token_response(expires_in=3600)])

    client._get_token()

    assert client._token_expires == 1000.0 + 3600 - 60


def test_rejected_credentials_raise_graph_error():
    client = make_client(token_responses=[FakeResponse(401, text="invalid_client")])

    with pytest.raises(GraphError, match=r"token request failed \(401\)") as excinfo:
        client.list_skus()

    assert excinfo.value.status == 401


def test_from_env_names_the_missing_settings(monkeypatch):
    monkeypatch.setattr(graph_api, "load_dotenv", lambda: None)   # ignore any real .env
    monkeypatch.setenv("TENANT_ID", "t")
    monkeypatch.delenv("CLIENT_ID", raising=False)
    monkeypatch.delenv("CLIENT_SECRET", raising=False)

    with pytest.raises(ConfigError, match="missing CLIENT_ID, CLIENT_SECRET"):
        GraphClient.from_env()


# --- retries ------------------------------------------------------------------

def test_throttling_waits_out_retry_after(sleeps):
    client = make_client([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, {"value": []}),
    ])

    client.list_skus()

    assert sleeps == [7]
    assert len(client.session.requests) == 2


def test_outage_without_retry_after_backs_off(sleeps):
    client = make_client([FakeResponse(503), FakeResponse(504), FakeResponse(200, {"value": []})])

    client.list_skus()

    assert sleeps == [2, 4]


def test_directory_concurrency_collision_is_retried(sleeps):
    collision = FakeResponse(409, text='{"error":{"code":"Directory_ConcurrencyViolation"}}')
    client = make_client([collision, FakeResponse(204)])

    client.update_user("user-1", {"jobTitle": "Manager"})

    assert len(client.session.requests) == 2


def test_an_ordinary_conflict_is_not_retried(sleeps):
    conflict = FakeResponse(409, {"error": {"code": "Request_BadRequest", "message": "already exists"}})
    client = make_client([conflict])

    with pytest.raises(GraphError, match="Request_BadRequest: already exists"):
        client.update_user("user-1", {"jobTitle": "Manager"})

    assert sleeps == []
    assert len(client.session.requests) == 1


def test_gives_up_after_three_attempts(sleeps):
    client = make_client([FakeResponse(429, headers={"Retry-After": "1"})] * 3)

    with pytest.raises(GraphError) as excinfo:
        client.list_skus()

    assert excinfo.value.status == 429
    assert len(client.session.requests) == 3
    assert len(sleeps) == 2   # no pointless wait after the final attempt


# --- errors -------------------------------------------------------------------

def test_graph_error_carries_code_message_and_status():
    denied = FakeResponse(403, {"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}})
    client = make_client([denied])

    with pytest.raises(GraphError) as excinfo:
        client.revoke_sessions("user-1")

    assert excinfo.value.status == 403
    assert "Authorization_RequestDenied: Insufficient privileges" in str(excinfo.value)


def test_non_json_error_body_falls_back_to_text():
    client = make_client([FakeResponse(500, text="upstream gateway exploded")])

    with pytest.raises(GraphError, match="upstream gateway exploded"):
        client.list_skus()


# --- the calls offboarding depends on -----------------------------------------

def test_member_groups_follows_paging_and_keeps_only_groups():
    next_link = f"{GRAPH_BASE}/users/user-1/memberOf?$skiptoken=abc"
    client = make_client([
        FakeResponse(200, {
            "value": [
                {"@odata.type": "#microsoft.graph.group", "id": "g1"},
                {"@odata.type": "#microsoft.graph.directoryRole", "id": "role"},
            ],
            "@odata.nextLink": next_link,
        }),
        FakeResponse(200, {"value": [{"@odata.type": "#microsoft.graph.group", "id": "g2"}]}),
    ])

    groups = client.get_member_groups("user-1")

    assert [group["id"] for group in groups] == ["g1", "g2"]
    # The nextLink is a full URL and must be used as-is, not re-prefixed.
    assert client.session.requests[1][1] == next_link


def test_lockout_calls_hit_the_right_endpoints():
    client = make_client([FakeResponse(204), FakeResponse(200, {"value": True})])

    client.update_user("user-1", {"accountEnabled": False})
    client.revoke_sessions("user-1")

    (m1, u1, _, k1), (m2, u2, _, _) = client.session.requests
    assert (m1, u1, k1["json"]) == ("PATCH", f"{GRAPH_BASE}/users/user-1", {"accountEnabled": False})
    assert (m2, u2) == ("POST", f"{GRAPH_BASE}/users/user-1/revokeSignInSessions")


def test_remove_licenses_sends_only_removals():
    client = make_client([FakeResponse(200, {})])

    client.remove_licenses("user-1", ["sku-a", "sku-b"])

    method, url, _, kwargs = client.session.requests[0]
    assert (method, url) == ("POST", f"{GRAPH_BASE}/users/user-1/assignLicense")
    assert kwargs["json"] == {"addLicenses": [], "removeLicenses": ["sku-a", "sku-b"]}


def test_remove_group_member_targets_the_membership_reference():
    client = make_client([FakeResponse(204)])

    client.remove_group_member("group-1", "user-1")

    method, url, _, _ = client.session.requests[0]
    assert (method, url) == ("DELETE", f"{GRAPH_BASE}/groups/group-1/members/user-1/$ref")
