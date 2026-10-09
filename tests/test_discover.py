"""Tests for the account lookup (provision.cmd_discover and print_user).

No network: the Graph client is replaced by FakeGraph, which answers
find_users from a list and records the fields each query asked for. The
reuse-or-new decision rests on what discover prints — whose name is on a
role account, whether it is enabled, and when it was last used — so that
output is what these tests pin down.

Run from the repo root:  python -m pytest -q
"""

import pytest

import provision
from graph_api import GraphError

DOMAIN = "example.com"


class FakeGraph:
    """find_users answers from `users`; .selects records every field list
    requested. With sign_in_readable=False the signInActivity select is
    refused with a 403, as it is without AuditLog.Read.All."""

    def __init__(self, users=(), sign_in_readable=True):
        self.users = list(users)
        self.sign_in_readable = sign_in_readable
        self.selects = []

    def find_users(self, prefix, select):
        self.selects.append(select)
        if "signInActivity" in select and not self.sign_in_readable:
            raise GraphError("Graph API error (403) — Authorization_RequestDenied", status=403)
        return [u for u in self.users if u["userPrincipalName"].startswith(prefix)]


def role_account(**overrides):
    user = {
        "id": "user-1",
        "displayName": "Property Manager at Elm Court",
        "userPrincipalName": f"manager619@{DOMAIN}",
        "accountEnabled": False,
        "jobTitle": "Property Manager",
        "givenName": "Pat",
        "surname": "Previous",
        "officeLocation": "Elm Court",
        "signInActivity": {"lastSignInDateTime": "2026-09-30T08:00:00Z"},
    }
    user.update(overrides)
    return user


@pytest.fixture
def wire(monkeypatch, tmp_path):
    monkeypatch.setattr(provision, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        provision, "load_config",
        lambda: {"tenant": {"domain": DOMAIN}, "naming": {"roles": ["manager", "leasing"]}},
    )

    def install(client):
        monkeypatch.setattr(provision.GraphClient, "from_env", classmethod(lambda cls: client))
        return client

    return install


def test_discover_shows_who_holds_a_role_account(wire, capsys):
    client = wire(FakeGraph([role_account()]))

    assert provision.main(["discover", "619"]) == 0

    out = capsys.readouterr().out
    # The display name is the role, so the person has to come from
    # givenName/surname — which the lookup must therefore request.
    assert "Property Manager at Elm Court" in out
    assert "[DISABLED]" in out
    assert "held by: Pat Previous" in out
    assert "office: Elm Court" in out
    assert "last sign-in: 2026-09-30T08:00:00Z" in out
    assert all("givenName" in sel and "surname" in sel for sel in client.selects)


def test_discover_checks_every_role_prefix_at_the_property(wire):
    client = wire(FakeGraph([]))

    provision.main(["discover", "619"])

    assert len(client.selects) == 2   # manager619, leasing619


def test_discover_degrades_when_sign_in_activity_is_not_readable(wire, capsys):
    client = wire(FakeGraph([role_account()], sign_in_readable=False))

    assert provision.main(["discover", "619"]) == 0

    out = capsys.readouterr().out
    assert "held by: Pat Previous" in out
    assert "last sign-in unavailable" in out
    # One refused attempt, then every later query leaves signInActivity out.
    assert "signInActivity" in client.selects[0]
    assert all("signInActivity" not in sel for sel in client.selects[1:])


def test_discover_suggests_new_when_nothing_exists(wire, capsys):
    wire(FakeGraph([]))

    provision.main(["discover", "619"])

    out = capsys.readouterr().out
    assert "No accounts found for: manager619, leasing619" in out
    assert "python provision.py new" in out


def test_print_user_without_holder_or_office_stays_one_line(capsys):
    provision.print_user({"displayName": "Taylor Example", "userPrincipalName": "t@x", "accountEnabled": True})

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert "held by" not in out


# --- last sign-in covers more than interactive sign-ins -----------------------------

def test_a_phone_mail_app_refreshing_tokens_counts_as_recent_use():
    user = {"signInActivity": {
        "lastSignInDateTime": "2026-03-01T08:00:00Z",              # last typed-in sign-in
        "lastNonInteractiveSignInDateTime": "2026-10-08T06:30:00Z",  # Outlook mobile, this week
    }}
    assert provision.last_sign_in(user) == "2026-10-08T06:30:00Z (non-interactive)"


def test_the_last_successful_sign_in_wins_when_it_is_the_latest():
    user = {"signInActivity": {
        "lastSignInDateTime": "2026-10-01T08:00:00Z",
        "lastSuccessfulSignInDateTime": "2026-10-09T08:00:00Z",
    }}
    assert provision.last_sign_in(user) == "2026-10-09T08:00:00Z (successful)"


def test_no_activity_at_all_is_none():
    assert provision.last_sign_in({}) is None
    assert provision.last_sign_in({"signInActivity": {"lastSignInDateTime": None}}) is None


def test_discover_prints_the_kind_of_sign_in(wire, capsys):
    wire(FakeGraph([role_account(signInActivity={
        "lastSignInDateTime": "2026-01-01T00:00:00Z",
        "lastNonInteractiveSignInDateTime": "2026-10-07T12:00:00Z",
    })]))

    provision.main(["discover", "619"])

    assert "last sign-in: 2026-10-07T12:00:00Z (non-interactive)" in capsys.readouterr().out
