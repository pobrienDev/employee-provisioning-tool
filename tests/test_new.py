"""Tests for account creation (provision.cmd_new).

No network: FakeGraph answers the UPN ladder and the same-name check from
dictionaries and records every write, so a test can assert both what a
`new` run creates and what it refuses to create.

Run from the repo root:  python -m pytest -q
"""

from datetime import datetime, timedelta, timezone

import pytest

import provision
from graph_api import GraphError

DOMAIN = "example.com"
HIRE = {
    "first_name": "Taylor",
    "last_name": "Example",
    "title": "Property Manager",
    "property_number": "619",
    "property_name": "Elm Court",
}
CONFIG = {"tenant": {"domain": DOMAIN, "usage_location": "US"}}


class FakeGraph:
    """get_user answers from `existing` (UPN -> display name); find_users_by_name
    from `same_name`; create_user records the payload and returns an id."""

    WRITES = {"create_user", "update_user", "assign_license", "add_group_member"}

    def __init__(self, existing=None, same_name=(), create_error=None):
        self.existing = dict(existing or {})
        self.same_name = list(same_name)
        self.create_error = create_error
        self.calls = []

    @property
    def writes(self):
        return [call for call in self.calls if call[0] in self.WRITES]

    def get_user(self, upn, select):
        self.calls.append(("get_user", upn))
        if upn in self.existing:
            return {"id": "other", "displayName": self.existing[upn], "userPrincipalName": upn}
        return None

    def find_users_by_name(self, given_name, surname, select):
        self.calls.append(("find_users_by_name", given_name, surname))
        return list(self.same_name)

    def address_holder(self, local, domain):
        self.calls.append(("address_holder", f"{local}@{domain}"))
        return None

    def create_user(self, payload):
        self.calls.append(("create_user", payload))
        if self.create_error:
            raise self.create_error
        return {"id": "new-1", "userPrincipalName": payload["userPrincipalName"]}

    def list_skus(self):
        return []

    def get_member_groups(self, user_id):
        return []

    def get_member_roles(self, user_id):
        return []


@pytest.fixture
def wire(monkeypatch, tmp_path):
    monkeypatch.setattr(provision, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(provision, "load_config", lambda: dict(CONFIG))
    monkeypatch.setattr(provision, "load_hire", lambda: dict(HIRE))
    monkeypatch.setattr(provision, "temp_password", lambda: "Temp-Pass-1!")

    def install(client):
        monkeypatch.setattr(provision.GraphClient, "from_env", classmethod(lambda cls: client))
        return client

    return install


def hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- a half-finished run must not become a second account ----------------------

def test_a_recent_account_with_the_same_name_stops_new(wire, capsys):
    leftover = {
        "id": "u-left", "userPrincipalName": f"texample@{DOMAIN}",
        "displayName": "Property Manager at Elm Court", "createdDateTime": hours_ago(2),
    }
    client = wire(FakeGraph(existing={f"texample@{DOMAIN}": "Property Manager at Elm Court"}, same_name=[leftover]))

    assert provision.main(["new"]) == 1

    assert client.writes == []
    err = capsys.readouterr().err
    assert f"an account for Taylor Example was created" in err
    assert "reuse --upn texample --yes --force" in err


def test_an_old_namesake_does_not_stop_new(wire):
    namesake = {"id": "u-old", "userPrincipalName": f"texample@{DOMAIN}", "createdDateTime": hours_ago(24 * 30)}
    client = wire(FakeGraph(existing={f"texample@{DOMAIN}": "Taylor Example"}, same_name=[namesake]))

    assert provision.main(["new"]) == 0

    created = [call for call in client.writes if call[0] == "create_user"]
    assert created and created[0][1]["userPrincipalName"] == f"taexample@{DOMAIN}"


def test_an_explicit_upn_skips_the_name_check(wire):
    leftover = {"id": "u-left", "userPrincipalName": f"texample@{DOMAIN}", "createdDateTime": hours_ago(1)}
    client = wire(FakeGraph(same_name=[leftover]))

    assert provision.main(["new", "--upn", "taylor.e"]) == 0

    assert not [call for call in client.calls if call[0] == "find_users_by_name"]


def test_duplicate_upn_from_graph_points_at_reuse(wire, capsys):
    error = GraphError(
        "Graph API error (400) — Request_BadRequest: Another object with the same "
        "value for property userPrincipalName already exists.", status=400,
    )
    wire(FakeGraph(create_error=error))

    assert provision.main(["new"]) == 1

    err = capsys.readouterr().err
    assert "texample@example.com already exists" in err
    assert "reuse --upn texample --yes --force" in err


# --- dry run --------------------------------------------------------------------------

def test_dry_run_writes_nothing_and_rehearses_the_email(wire, monkeypatch, capsys):
    client = wire(FakeGraph())
    drafts = []
    monkeypatch.setattr(provision, "email_draft", lambda *a, **k: drafts.append(k))

    assert provision.main(["new", "--dry-run", "--open-draft"]) == 0

    assert client.writes == []
    assert "[dry-run] would create Property Manager at Elm Court (texample@example.com)" in capsys.readouterr().out
    # The draft step is told it is a rehearsal, so it can't sign in or write a draft.
    assert drafts and drafts[0]["dry"] is True and drafts[0]["open_draft"] is True
