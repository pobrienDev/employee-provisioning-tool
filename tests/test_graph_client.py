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
        self.puts = []

    def post(self, url, data=None, timeout=None):
        self.token_posts += 1
        self.last_token_request = (url, data)
        return self.token_responses.pop(0)

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method, url, dict(headers or {}), kwargs))
        return self.responses.pop(0)

    def put(self, url, data=None, headers=None, timeout=None):
        """Upload-session ranges go straight to the pre-authenticated URL."""
        self.puts.append((url, dict(headers or {}), len(data)))
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
    monkeypatch.setattr(graph_api, "load_dotenv", lambda *a, **k: None)   # ignore any real .env
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


# --- MFA method deletion ------------------------------------------------------

def test_auth_method_delete_hits_the_typed_endpoint():
    client = make_client([FakeResponse(204)])

    client.delete_auth_method("user-1", "phoneMethods", "m-1")

    method, url, _, _ = client.session.requests[0]
    assert (method, url) == ("DELETE", f"{GRAPH_BASE}/users/user-1/authentication/phoneMethods/m-1")


def test_qr_code_pin_is_a_singleton_without_an_id():
    client = make_client([FakeResponse(204)])

    client.delete_auth_method("user-1", "qrCodePinMethod", "ignored")

    _, url, _, _ = client.session.requests[0]
    assert url == f"{GRAPH_BASE}/users/user-1/authentication/qrCodePinMethod"


def test_every_v1_method_type_except_password_has_a_delete_path():
    documented = {
        "emailAuthenticationMethod", "externalAuthenticationMethod",
        "fido2AuthenticationMethod", "microsoftAuthenticatorAuthenticationMethod",
        "phoneAuthenticationMethod", "platformCredentialAuthenticationMethod",
        "qrCodePinAuthenticationMethod", "softwareOathAuthenticationMethod",
        "temporaryAccessPassAuthenticationMethod",
        "windowsHelloForBusinessAuthenticationMethod",
    }
    mapped = {key.split(".")[-1] for key in graph_api.AUTH_METHOD_PATHS}
    assert mapped == documented
    assert "passwordAuthenticationMethod" not in mapped


def test_member_roles_keeps_only_directory_roles():
    client = make_client([
        FakeResponse(200, {
            "value": [
                {"@odata.type": "#microsoft.graph.group", "id": "g1"},
                {"@odata.type": "#microsoft.graph.directoryRole", "id": "role", "displayName": "User Administrator"},
                {"@odata.type": "#microsoft.graph.administrativeUnit", "id": "au"},
            ],
        }),
    ])

    roles = client.get_member_roles("user-1")

    assert [role["id"] for role in roles] == ["role"]
    _, url, _, _ = client.session.requests[0]
    assert url == f"{GRAPH_BASE}/users/user-1/memberOf?$select=id,displayName"


def test_env_file_next_to_the_module_wins_over_the_shell(monkeypatch, tmp_path):
    """A TENANT_ID left exported from an earlier session must not redirect
    a run away from the tenant .env names."""
    env_file = tmp_path / ".env"
    env_file.write_text("TENANT_ID=from-file\nCLIENT_ID=c\nCLIENT_SECRET=s\n", encoding="utf-8")
    monkeypatch.setattr(graph_api, "ENV_FILE", env_file)
    monkeypatch.setenv("TENANT_ID", "from-shell")

    client = GraphClient.from_env()

    assert client.tenant_id == "from-file"


def test_without_an_env_file_the_environment_is_used(monkeypatch, tmp_path):
    monkeypatch.setattr(graph_api, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv("TENANT_ID", "t")
    monkeypatch.setenv("CLIENT_ID", "c")
    monkeypatch.setenv("CLIENT_SECRET", "s")

    assert GraphClient.from_env().tenant_id == "t"


# --- the delegated refresh token cache ------------------------------------------

import json
import os

from graph_api import DelegatedGraphClient, forget_sign_in


@pytest.fixture
def token_paths(monkeypatch, tmp_path):
    new = tmp_path / "profile" / "token_cache.json"
    legacy = tmp_path / "repo" / ".token_cache.json"
    legacy.parent.mkdir()
    monkeypatch.setattr(graph_api, "TOKEN_CACHE", new)
    monkeypatch.setattr(graph_api, "LEGACY_TOKEN_CACHE", legacy)
    return new, legacy


def test_refresh_token_is_stored_under_the_profile_owner_only(token_paths):
    new, legacy = token_paths
    legacy.write_text('{"refresh_token": "old"}', encoding="utf-8")
    client = DelegatedGraphClient("tenant-id", "client-id", None)

    client._store({"access_token": "a", "expires_in": 3600, "refresh_token": "fresh"})

    assert json.loads(new.read_text(encoding="utf-8")) == {"refresh_token": "fresh"}
    assert not legacy.exists()   # the copy inside the repo folder is gone
    if os.name != "nt":
        assert oct(new.stat().st_mode & 0o777) == "0o600"


def test_a_cache_left_by_an_older_version_is_still_honored(token_paths):
    new, legacy = token_paths
    legacy.write_text('{"refresh_token": "old"}', encoding="utf-8")

    assert DelegatedGraphClient._cached_refresh_token() == "old"


def test_sign_out_removes_both_cache_locations(token_paths):
    new, legacy = token_paths
    new.parent.mkdir()
    new.write_text("{}", encoding="utf-8")
    legacy.write_text("{}", encoding="utf-8")

    removed = forget_sign_in()

    assert set(removed) == {str(new), str(legacy)}
    assert not new.exists() and not legacy.exists()
    assert forget_sign_in() == []


# --- creates are never blindly retried ----------------------------------------

def test_create_user_is_not_retried_after_a_gateway_timeout(sleeps):
    client = make_client([FakeResponse(504, text="gateway timeout")])

    with pytest.raises(GraphError, match="may already have been applied") as excinfo:
        client.create_user({"userPrincipalName": "t@x"})

    assert excinfo.value.status == 504
    assert len(client.session.requests) == 1
    assert sleeps == []


def test_create_user_still_retries_throttling(sleeps):
    client = make_client([
        FakeResponse(429, headers={"Retry-After": "1"}),
        FakeResponse(201, {"id": "u1"}),
    ])

    assert client.create_user({"userPrincipalName": "t@x"}) == {"id": "u1"}
    assert len(client.session.requests) == 2


def test_create_draft_is_not_retried_after_an_outage(sleeps):
    client = make_client([FakeResponse(503)])

    with pytest.raises(GraphError):
        client.create_draft({"subject": "x"})

    assert len(client.session.requests) == 1


def test_idempotent_writes_still_retry_outages(sleeps):
    client = make_client([FakeResponse(503), FakeResponse(204)])

    client.update_user("user-1", {"jobTitle": "Manager"})

    assert len(client.session.requests) == 2


# --- address collisions beyond the UPN -----------------------------------------

def test_address_holder_checks_mail_aliases_and_nickname_on_users_then_groups():
    client = make_client([
        FakeResponse(200, {"value": []}),
        FakeResponse(200, {"value": [{"displayName": "Sales Team", "mail": "sales@d.com"}]}),
    ])

    assert client.address_holder("sales", "d.com") == "Sales Team, sales@d.com"

    (_, u1, _, k1), (_, u2, _, k2) = client.session.requests
    assert (u1, u2) == (f"{GRAPH_BASE}/users", f"{GRAPH_BASE}/groups")
    for kwargs in (k1, k2):
        flt = kwargs["params"]["$filter"]
        assert "mail eq 'sales@d.com'" in flt
        assert "mailNickname eq 'sales'" in flt
        assert "proxyAddresses/any(p:p eq 'smtp:sales@d.com')" in flt
        assert "proxyAddresses/any(p:p eq 'SMTP:sales@d.com')" in flt


def test_address_holder_names_the_user_holding_an_alias():
    client = make_client([
        FakeResponse(200, {"value": [{"displayName": "Property Manager at Elm Court", "userPrincipalName": "manager536@d.com"}]}),
    ])

    assert client.address_holder("tsmith", "d.com") == "Property Manager at Elm Court, manager536@d.com"
    assert len(client.session.requests) == 1   # found on users; groups not queried


def test_address_holder_escapes_apostrophes_in_the_filter():
    client = make_client([FakeResponse(200, {"value": []}), FakeResponse(200, {"value": []})])

    assert client.address_holder("o'brien", "d.com") is None

    assert "mailNickname eq 'o''brien'" in client.session.requests[0][3]["params"]["$filter"]


# --- draft attachments: small files inline, large ones by upload session ----------

from graph_api import ATTACHMENT_INLINE_LIMIT, UPLOAD_CHUNK


def test_a_small_file_is_attached_in_one_post():
    client = make_client([FakeResponse(201, {"id": "att-1"})])

    client.add_file_attachment("msg-1", "notes.pdf", b"x" * 1000)

    method, url, headers, kwargs = client.session.requests[0]
    assert (method, url) == ("POST", f"{GRAPH_BASE}/me/messages/msg-1/attachments")
    assert kwargs["json"]["name"] == "notes.pdf"
    assert kwargs["json"]["contentBytes"]
    assert "contentId" not in kwargs["json"]
    assert client.session.puts == []


def test_a_large_file_goes_through_an_upload_session_in_ranges():
    data = b"y" * (ATTACHMENT_INLINE_LIMIT + 1)   # exactly one byte past the inline limit
    client = make_client([
        FakeResponse(201, {"uploadUrl": "https://outlook.office.com/upload?authtoken=t"}),
        FakeResponse(200, {"nextExpectedRanges": [str(UPLOAD_CHUNK)]}),
        FakeResponse(201),
    ])

    client.add_file_attachment("msg-1", "MFA Instructions.pdf", data)

    method, url, _, kwargs = client.session.requests[0]
    assert (method, url) == ("POST", f"{GRAPH_BASE}/me/messages/msg-1/attachments/createUploadSession")
    assert kwargs["json"] == {"AttachmentItem": {"attachmentType": "file", "name": "MFA Instructions.pdf", "size": len(data)}}
    # Two ranges, in order, each labelled with its byte span and the total...
    (u1, h1, n1), (u2, h2, n2) = client.session.puts
    assert u1 == u2 == "https://outlook.office.com/upload?authtoken=t"
    assert (n1, n2) == (UPLOAD_CHUNK, 1)
    assert h1["Content-Range"] == f"bytes 0-{UPLOAD_CHUNK - 1}/{len(data)}"
    assert h2["Content-Range"] == f"bytes {UPLOAD_CHUNK}-{UPLOAD_CHUNK}/{len(data)}"
    # ...on the pre-authenticated URL, so no bearer token goes along.
    assert "Authorization" not in h1 and "Authorization" not in h2


def test_an_inline_signature_image_keeps_its_content_id_on_both_paths():
    client = make_client([
        FakeResponse(201, {"id": "att"}),
        FakeResponse(201, {"uploadUrl": "https://outlook.office.com/upload"}),
        FakeResponse(201),
    ])

    client.add_file_attachment("m", "logo.png", b"s", content_type="image/png", content_id="logo", is_inline=True)
    client.add_file_attachment("m", "big.png", b"b" * ATTACHMENT_INLINE_LIMIT, content_type="image/png", content_id="big", is_inline=True)

    small = client.session.requests[0][3]["json"]
    assert (small["contentId"], small["isInline"], small["contentType"]) == ("logo", True, "image/png")
    large = client.session.requests[1][3]["json"]["AttachmentItem"]
    assert (large["contentId"], large["isInline"], large["contentType"]) == ("big", True, "image/png")


def test_a_rejected_range_is_a_graph_error():
    client = make_client([
        FakeResponse(201, {"uploadUrl": "https://outlook.office.com/upload"}),
        FakeResponse(413, text="too big"),
    ])

    with pytest.raises(GraphError, match="upload of big.pdf failed"):
        client.add_file_attachment("m", "big.pdf", b"b" * ATTACHMENT_INLINE_LIMIT)


# --- retry gaps: connection errors, HTTP-date Retry-After, the wait cap ------------

import requests as _requests
from email.utils import format_datetime
from datetime import datetime as _dt, timedelta as _td, timezone as _tz


class FlakySession(FakeSession):
    """Raises a connection error for the first `failures` Graph requests."""

    def __init__(self, responses, failures):
        super().__init__(responses)
        self.failures = failures

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method, url, dict(headers or {}), kwargs))
        if self.failures:
            self.failures -= 1
            raise _requests.ConnectionError("connection reset by peer")
        return self.responses.pop(0)


def test_a_dropped_connection_is_retried_for_idempotent_requests(sleeps):
    client = GraphClient("tenant-id", "client-id", "client-secret")
    client.session = FlakySession([FakeResponse(200, {"value": []})], failures=1)

    client.list_skus()

    assert len(client.session.requests) == 2
    assert sleeps == [2]


def test_a_dropped_connection_is_not_retried_for_a_create(sleeps):
    client = GraphClient("tenant-id", "client-id", "client-secret")
    client.session = FlakySession([FakeResponse(201, {"id": "u"})], failures=1)

    with pytest.raises(GraphError, match="connection reset"):
        client.create_user({"userPrincipalName": "t@x"})

    assert len(client.session.requests) == 1 and sleeps == []


def test_a_connection_that_never_recovers_gives_up_after_max_attempts(sleeps):
    client = GraphClient("tenant-id", "client-id", "client-secret")
    client.session = FlakySession([], failures=99)

    with pytest.raises(GraphError):
        client.list_skus()

    assert len(client.session.requests) == 3


def test_retry_after_as_an_http_date_is_honored(sleeps):
    when = _dt.now(_tz.utc) + _td(seconds=30)
    client = make_client([
        FakeResponse(429, headers={"Retry-After": format_datetime(when, usegmt=True)}),
        FakeResponse(200, {"value": []}),
    ])

    client.list_skus()

    assert len(sleeps) == 1 and 25 <= sleeps[0] <= 30


def test_an_absurd_retry_after_is_capped(sleeps):
    client = make_client([FakeResponse(429, headers={"Retry-After": "86400"}), FakeResponse(200, {"value": []})])

    client.list_skus()

    assert sleeps == [120]


def test_an_unparseable_retry_after_falls_back_to_backoff(sleeps):
    client = make_client([FakeResponse(429, headers={"Retry-After": "soon"}), FakeResponse(200, {"value": []})])

    client.list_skus()

    assert sleeps == [2]


def test_attempt_count_is_configurable(sleeps):
    client = make_client([FakeResponse(429, headers={"Retry-After": "1"})] * 5)
    client.max_attempts = 5

    with pytest.raises(GraphError):
        client.list_skus()

    assert len(client.session.requests) == 5


# --- every list call follows paging -------------------------------------------------

def test_find_users_follows_paging_without_resending_the_filter():
    next_link = f"{GRAPH_BASE}/users?$skiptoken=page2"
    client = make_client([
        FakeResponse(200, {"value": [{"id": "u1"}], "@odata.nextLink": next_link}),
        FakeResponse(200, {"value": [{"id": "u2"}]}),
    ])

    users = client.find_users("manager", "id")

    assert [u["id"] for u in users] == ["u1", "u2"]
    (_, u1, _, k1), (_, u2, _, k2) = client.session.requests
    assert u1 == f"{GRAPH_BASE}/users" and "$filter" in k1["params"]
    # The nextLink already carries the filter and skiptoken; no params go with it.
    assert u2 == next_link and k2["params"] is None


@pytest.mark.parametrize("call, path", [
    (lambda c: c.list_skus(), "/subscribedSkus"),
    (lambda c: c.list_auth_methods("user-1"), "/users/user-1/authentication/methods"),
    (lambda c: c.find_drafts("signature-capture"), "/me/mailFolders/drafts/messages"),
    (lambda c: c.find_users_by_name("A", "B", "id"), "/users"),
])
def test_list_calls_read_past_the_first_page(call, path):
    next_link = f"{GRAPH_BASE}{path}?$skiptoken=x"
    client = make_client([
        FakeResponse(200, {"value": [{"id": 1}], "@odata.nextLink": next_link}),
        FakeResponse(200, {"value": [{"id": 2}]}),
    ])

    assert [item["id"] for item in call(client)] == [1, 2]
    assert client.session.requests[1][1] == next_link
