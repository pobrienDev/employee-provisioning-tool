"""Tests for the role-account handover (provision.cmd_reuse).

Zero network calls: Microsoft Graph is replaced by FakeGraph, a recording
stand-in like the one in test_terminate.py, so a test can assert the exact
sequence of writes. The sequence is the point of the command: lock the
previous holder out, wipe their MFA registrations, and only then rename
and re-enable the account for the new hire.

hire.yaml, config.yaml, .env and the logs directory are all replaced per
test, so nothing here reads or writes real project files.

Run from the repo root:  python -m pytest -q
"""

import pytest

import provision
from graph_api import GraphError

DOMAIN = "example.com"
UPN = f"manager619@{DOMAIN}"
USER_ID = "user-1"

PHONE = {"@odata.type": "#microsoft.graph.phoneAuthenticationMethod", "id": "m-phone"}
AUTHENTICATOR = {
    "@odata.type": "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod",
    "id": "m-app",
}


class FakeGraph:
    """In-memory stand-in for graph_api.GraphClient that records every call.

      .calls         every method call as a tuple, in order
      .writes        just the calls that would change the tenant
      deny_password  the password reset raises a 403, as it does when the
                     app lacks User-PasswordProfile.ReadWrite.All
      methods        the MFA methods list_auth_methods hands back
    """

    WRITES = {
        "update_user", "revoke_sessions", "delete_auth_method",
        "assign_license", "add_group_member",
    }

    def __init__(self, user=None, methods=(), deny_password=False, skus=()):
        self.user = user
        self.methods = list(methods)
        self.deny_password = deny_password
        self.skus = list(skus)
        self.calls = []

    @property
    def writes(self):
        return [call for call in self.calls if call[0] in self.WRITES]

    def get_user(self, upn_or_id, select):
        self.calls.append(("get_user", upn_or_id))
        return self.user

    def update_user(self, user_id, changes):
        self.calls.append(("update_user", user_id, changes))
        if self.deny_password and "passwordProfile" in changes:
            raise GraphError("Graph API error (403) — Authorization_RequestDenied", status=403)

    def revoke_sessions(self, user_id):
        self.calls.append(("revoke_sessions", user_id))

    def list_auth_methods(self, user_id):
        self.calls.append(("list_auth_methods", user_id))
        return list(self.methods)

    def delete_auth_method(self, user_id, method_path, method_id):
        self.calls.append(("delete_auth_method", user_id, method_path, method_id))

    def list_skus(self):
        self.calls.append(("list_skus",))
        return list(self.skus)

    def assign_license(self, user_id, sku_id):
        self.calls.append(("assign_license", user_id, sku_id))

    def get_group(self, group_id, select="displayName"):
        self.calls.append(("get_group", group_id))
        return None

    def add_group_member(self, group_id, user_id):
        self.calls.append(("add_group_member", group_id, user_id))


def make_user(enabled=False):
    return {
        "id": USER_ID,
        "displayName": "Property Manager at Elm Court",
        "givenName": "Pat",
        "surname": "Previous",
        "userPrincipalName": UPN,
        "accountEnabled": enabled,
        "jobTitle": "Property Manager",
    }


HIRE = {
    "first_name": "Taylor",
    "last_name": "Example",
    "title": "Property Manager",
    "property_number": "619",
    "property_name": "Elm Court",
    "reuse_upn": "manager619",
}

CONFIG = {"tenant": {"domain": DOMAIN}, "naming": {"roles": ["manager", "leasing"]}}


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Point provision at a fake client, hire, config, and a temp log dir."""
    monkeypatch.setattr(provision, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(provision, "load_config", lambda: dict(CONFIG))
    monkeypatch.setattr(provision, "load_hire", lambda: dict(HIRE))
    monkeypatch.setattr(provision, "temp_password", lambda: "Temp-Pass-1!")

    def install(client):
        monkeypatch.setattr(provision.GraphClient, "from_env", classmethod(lambda cls: client))
        return client

    return install


def audit_text(tmp_path):
    logs = list((tmp_path / "logs").glob("provision-*.log"))
    assert len(logs) == 1
    return logs[0].read_text(encoding="utf-8")


# --- the order of writes --------------------------------------------------------

def test_reuse_wipes_mfa_before_enabling_the_account(wire):
    client = wire(FakeGraph(make_user(), methods=[PHONE, AUTHENTICATOR]))

    assert provision.main(["reuse"]) == 0

    kinds = [call[0] for call in client.writes]
    # Lock out first (password, then sessions), wipe MFA, and only then
    # rename and enable: the account must never be enabled while the
    # previous holder's phone or Authenticator is still registered.
    assert kinds == [
        "update_user", "revoke_sessions",
        "delete_auth_method", "delete_auth_method",
        "update_user",
    ]
    assert "passwordProfile" in client.writes[0][2]
    assert client.writes[0][2]["passwordProfile"]["forceChangePasswordNextSignIn"] is True
    assert client.writes[-1][2]["accountEnabled"] is True
    assert client.writes[-1][2]["displayName"] == "Property Manager at Elm Court"
    deleted = [(call[2], call[3]) for call in client.writes if call[0] == "delete_auth_method"]
    assert deleted == [("phoneMethods", "m-phone"), ("microsoftAuthenticatorMethods", "m-app")]
