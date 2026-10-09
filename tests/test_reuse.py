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
PASSWORD = {"@odata.type": "#microsoft.graph.passwordAuthenticationMethod", "id": "m-pw"}
QR_PIN = {"@odata.type": "#microsoft.graph.qrCodePinAuthenticationMethod", "id": "m-qr"}
UNKNOWN = {"@odata.type": "#microsoft.graph.futureAuthenticationMethod", "id": "m-new"}
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
      deny_methods   list_auth_methods raises a 403, as it does when the
                     app lacks UserAuthenticationMethod.ReadWrite.All
      methods        the MFA methods list_auth_methods hands back
      group_lookup   group id -> the group get_group returns, or an
                     exception for it to raise; ids not listed are None
    """

    WRITES = {
        "update_user", "revoke_sessions", "delete_auth_method",
        "assign_license", "add_group_member",
    }

    def __init__(self, user=None, methods=(), deny_password=False, deny_methods=False,
                 skus=(), groups=(), roles=(), group_lookup=None):
        self.user = user
        self.methods = list(methods)
        self.deny_password = deny_password
        self.deny_methods = deny_methods
        self.skus = list(skus)
        self.groups = list(groups)
        self.roles = list(roles)
        self.group_lookup = dict(group_lookup or {})
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
        if self.deny_methods:
            raise GraphError("Graph API error (403) — Authorization_RequestDenied", status=403)
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
        answer = self.group_lookup.get(group_id)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def add_group_member(self, group_id, user_id):
        self.calls.append(("add_group_member", group_id, user_id))

    def get_member_groups(self, user_id):
        self.calls.append(("get_member_groups", user_id))
        return list(self.groups)

    def get_member_roles(self, user_id):
        self.calls.append(("get_member_roles", user_id))
        return list(self.roles)


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

    assert provision.main(["reuse", "--yes"]) == 0

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


# --- preview, dry-run and the wrong-account guard ------------------------------

def test_without_yes_nothing_is_written(wire, capsys, tmp_path):
    client = wire(FakeGraph(make_user(), methods=[PHONE]))

    assert provision.main(["reuse"]) == 0

    assert client.writes == []
    out = capsys.readouterr().out
    assert "Reusing manager619@example.com" in out
    assert "[DISABLED]" in out
    assert "plan: reset the password, revoke sessions" in out
    assert "re-run with --yes" in out
    assert "preview only" in audit_text(tmp_path)


def test_dry_run_writes_nothing_and_says_what_it_would_do(wire, capsys):
    client = wire(FakeGraph(make_user(), methods=[PHONE, AUTHENTICATOR]))

    assert provision.main(["reuse", "--dry-run"]) == 0

    assert client.writes == []
    out = capsys.readouterr().out
    assert "[dry-run] would reset the password and revoke sessions" in out
    assert "[dry-run] would remove 2 registered mfa method(s)" in out
    assert "[dry-run] would rename to Property Manager at Elm Court and enable the account" in out


def test_an_enabled_account_is_refused_without_force(wire, capsys):
    user = make_user(enabled=True)
    user["signInActivity"] = {"lastSignInDateTime": "2026-10-08T15:04:05Z"}
    client = wire(FakeGraph(user, methods=[PHONE]))

    assert provision.main(["reuse", "--yes"]) == 1

    assert client.writes == []
    captured = capsys.readouterr()
    assert "warning: the account is enabled" in captured.out
    assert "last sign-in 2026-10-08T15:04:05Z" in captured.out
    assert "refusing to reuse this account" in captured.err
    assert "--force" in captured.err


def test_a_non_role_upn_is_refused_without_force(wire, capsys):
    client = wire(FakeGraph(make_user(), methods=[]))

    # A typo — tsmith instead of tsmith2 — must not lock a real person out.
    assert provision.main(["reuse", "--upn", "tsmith", "--yes"]) == 1

    assert client.writes == []
    assert "tsmith is not a role account" in capsys.readouterr().out


def test_a_role_account_at_another_property_is_refused_without_force(wire, capsys):
    client = wire(FakeGraph(make_user(), methods=[]))

    assert provision.main(["reuse", "--upn", "manager536", "--yes"]) == 1

    assert client.writes == []
    assert "manager536 belongs to property 536, but hire.yaml says 619" in capsys.readouterr().out


def test_force_overrides_the_guard(wire):
    client = wire(FakeGraph(make_user(enabled=True), methods=[]))

    assert provision.main(["reuse", "--yes", "--force"]) == 0

    assert [call[0] for call in client.writes] == ["update_user", "revoke_sessions", "update_user"]


def test_sign_in_activity_degrades_when_not_readable(wire):
    """Without AuditLog.Read.All the signInActivity select is refused;
    the lookup must fall back to the plain fields rather than fail."""

    class NoSignIn(FakeGraph):
        def get_user(self, upn_or_id, select):
            self.calls.append(("get_user", upn_or_id, select))
            if "signInActivity" in select:
                raise GraphError("Graph API error (403) — Authorization_RequestDenied", status=403)
            return self.user

    client = wire(NoSignIn(make_user(), methods=[]))

    assert provision.main(["reuse", "--yes"]) == 0

    selects = [call[2] for call in client.calls if call[0] == "get_user"]
    assert "signInActivity" in selects[0] and "signInActivity" not in selects[1]


# --- the MFA wipe must never fail quietly -------------------------------------

def test_missing_mfa_permission_is_reported_and_exits_1(wire, capsys, tmp_path):
    client = wire(FakeGraph(make_user(), deny_methods=True))

    assert provision.main(["reuse", "--yes"]) == 1

    # The handover itself still happens; what must not happen is a clean
    # exit 0 while the previous holder's phone can still answer MFA prompts.
    assert [call[0] for call in client.writes] == ["update_user", "revoke_sessions", "update_user"]
    err = capsys.readouterr().err
    assert "completed with issues" in err
    assert "mfa not reset" in err and "UserAuthenticationMethod.ReadWrite.All" in err
    assert "mfa not reset" in audit_text(tmp_path)


def test_every_documented_method_type_is_removed_and_the_password_is_left(wire):
    client = wire(FakeGraph(make_user(), methods=[PASSWORD, PHONE, QR_PIN]))

    assert provision.main(["reuse", "--yes"]) == 0

    deleted = [call[2] for call in client.writes if call[0] == "delete_auth_method"]
    assert deleted == ["phoneMethods", "qrCodePinMethod"]


def test_a_method_type_the_tool_cannot_remove_is_an_issue(wire, capsys):
    client = wire(FakeGraph(make_user(), methods=[UNKNOWN]))

    assert provision.main(["reuse", "--yes"]) == 1

    assert not [call for call in client.writes if call[0] == "delete_auth_method"]
    captured = capsys.readouterr()
    assert "mfa method future left in place" in captured.out
    assert "no registered mfa methods to remove" in captured.out
    assert "completed with issues" in captured.err


def test_a_failed_method_delete_is_an_issue_but_the_rest_continue(wire, capsys):
    class FlakyGraph(FakeGraph):
        def delete_auth_method(self, user_id, method_path, method_id):
            super().delete_auth_method(user_id, method_path, method_id)
            if method_id == "m-phone":
                raise GraphError("Graph API error (500) — boom", status=500)

    client = wire(FlakyGraph(make_user(), methods=[PHONE, AUTHENTICATOR]))

    assert provision.main(["reuse", "--yes"]) == 1

    deleted = [call[3] for call in client.writes if call[0] == "delete_auth_method"]
    assert deleted == ["m-phone", "m-app"]
    assert "could not remove mfa method phone" in capsys.readouterr().err


# --- what the previous holder leaves behind ------------------------------------

MANAGERS_ID = "00000000-0000-0000-0000-00000000000a"
MANAGERS = {"id": MANAGERS_ID, "displayName": "Site Managers", "groupTypes": [], "mailEnabled": False}
MAINTENANCE = {"id": "g-maint", "displayName": "Maintenance", "groupTypes": [], "mailEnabled": False}
ALL_STAFF_DL = {
    "id": "g-dl", "displayName": "All Staff", "groupTypes": [],
    "mailEnabled": True, "mail": "allstaff@example.com",
}


def test_memberships_outside_the_hire_mapping_are_listed_for_review(wire, monkeypatch, capsys, tmp_path):
    config = dict(CONFIG, groups={"titles": {"Property Manager": [MANAGERS_ID]}})
    monkeypatch.setattr(provision, "load_config", lambda: config)
    client = wire(FakeGraph(
        make_user(), groups=[MANAGERS, MAINTENANCE, ALL_STAFF_DL],
        group_lookup={MANAGERS_ID: MANAGERS},
    ))

    assert provision.main(["reuse", "--yes"]) == 0

    out = capsys.readouterr().out
    assert "review — 2 membership(s) kept from the previous holder" in out
    assert "still a member of Maintenance (remove in the admin center" in out
    assert "still a member of All Staff (distribution list" in out
    assert "still a member of Site Managers" not in out
    # Listed, never removed: some of them may be intended.
    assert not [call for call in client.calls if call[0] == "remove_group_member"]
    assert "still a member of Maintenance" in audit_text(tmp_path)


def test_an_inherited_directory_role_is_an_issue(wire, capsys):
    client = wire(FakeGraph(make_user(), roles=[{"id": "r-1", "displayName": "Groups Administrator"}]))

    assert provision.main(["reuse", "--yes"]) == 1

    assert "directory role Groups Administrator inherited from the previous holder" in capsys.readouterr().err


# --- one bad item must not abandon the rest ------------------------------------

GOOD_ID = "00000000-0000-0000-0000-000000000005"
OTHER_ID = "00000000-0000-0000-0000-000000000006"
SITE_STAFF = {"id": GOOD_ID, "displayName": "Site Staff", "groupTypes": [], "mailEnabled": False}


def with_groups(monkeypatch, *ids):
    config = dict(CONFIG, groups={"titles": {"Property Manager": list(ids)}})
    monkeypatch.setattr(provision, "load_config", lambda: config)


def test_a_non_guid_group_id_in_config_is_reported_and_the_rest_still_join(wire, monkeypatch, capsys):
    with_groups(monkeypatch, "Site Staff", GOOD_ID)
    client = wire(FakeGraph(make_user(), group_lookup={GOOD_ID: SITE_STAFF}))

    assert provision.main(["reuse", "--yes"]) == 1

    assert ("add_group_member", GOOD_ID, USER_ID) in client.writes
    assert not [call for call in client.calls if call[0] == "get_group" and call[1] == "Site Staff"]
    assert "group Site Staff is not a group ID" in capsys.readouterr().err


def test_a_group_lookup_error_is_an_issue_not_an_abort(wire, monkeypatch, capsys):
    with_groups(monkeypatch, OTHER_ID, GOOD_ID)
    bad = GraphError("Graph API error (400) — Request_BadRequest: Invalid object identifier", status=400)
    client = wire(FakeGraph(make_user(), group_lookup={OTHER_ID: bad, GOOD_ID: SITE_STAFF}))

    assert provision.main(["reuse", "--yes"]) == 1

    assert ("add_group_member", GOOD_ID, USER_ID) in client.writes
    err = capsys.readouterr().err
    assert "completed with issues" in err
    assert f"could not look up group {OTHER_ID}" in err


def test_a_license_lookup_error_is_an_issue_and_groups_still_join(wire, monkeypatch, capsys):
    config = dict(CONFIG, licensing={"default": ["SPB"]}, groups={"titles": {"Property Manager": [GOOD_ID]}})
    monkeypatch.setattr(provision, "load_config", lambda: config)

    class NoSkus(FakeGraph):
        def list_skus(self):
            raise GraphError("Graph API error (500) — InternalServerError", status=500)

    client = wire(NoSkus(make_user(), group_lookup={GOOD_ID: SITE_STAFF}))

    assert provision.main(["reuse", "--yes"]) == 1

    assert ("add_group_member", GOOD_ID, USER_ID) in client.writes
    assert "could not read the tenant's license SKUs" in capsys.readouterr().err


def test_an_auth_method_lookup_error_is_an_issue_not_an_abort(wire, capsys):
    class NoMethods(FakeGraph):
        def list_auth_methods(self, user_id):
            raise GraphError("Graph API error (500) — InternalServerError", status=500)

    client = wire(NoMethods(make_user()))

    assert provision.main(["reuse", "--yes"]) == 1

    # The rename/enable still happened and the summary still printed.
    assert client.writes[-1][2]["accountEnabled"] is True
    err = capsys.readouterr().err
    assert "could not read the account's authentication methods" in err


# --- every write is logged (and the password shown) as it lands ----------------

def test_a_failed_rename_still_logs_the_reset_and_shows_the_password(wire, capsys, tmp_path):
    class RenameFails(FakeGraph):
        def update_user(self, user_id, changes):
            super().update_user(user_id, changes)
            if "accountEnabled" in changes:
                raise GraphError("Graph API error (503) — ServiceUnavailable", status=503)

    client = wire(RenameFails(make_user(), methods=[PHONE]))

    assert provision.main(["reuse", "--yes"]) == 1

    # Password reset, sessions revoked and MFA wiped all happened...
    assert [call[0] for call in client.writes] == [
        "update_user", "revoke_sessions", "delete_auth_method", "update_user",
    ]
    log = audit_text(tmp_path)
    # ...and each is on record, before the error, with the password shown.
    assert log.index("password reset") < log.index("sessions revoked") < log.index("error (reuse)")
    assert "temp password: Temp-Pass-1!" in capsys.readouterr().out


def test_a_denied_password_reset_changes_nothing(wire, capsys):
    client = wire(FakeGraph(make_user(), methods=[PHONE], deny_password=True))

    assert provision.main(["reuse", "--yes"]) == 1

    assert [call[0] for call in client.writes] == ["update_user"]   # the refused reset only
    captured = capsys.readouterr()
    assert "Nothing was changed" in captured.err
    assert "temp password" not in captured.out


# --- a reused account's existing license ---------------------------------------

def sku(part, sku_id, free=5):
    return {"skuId": sku_id, "skuPartNumber": part, "prepaidUnits": {"enabled": 10}, "consumedUnits": 10 - free}


SKUS = [sku("SPB", "sku-spb"), sku("STANDARDPACK", "sku-e1"), sku("VISIOCLIENT", "sku-visio")]


def licensed_user(*sku_ids):
    user = make_user()
    user["assignedLicenses"] = [{"skuId": s} for s in sku_ids]
    return user


def with_chain(monkeypatch, *chain):
    config = dict(CONFIG, licensing={"default": list(chain)})
    monkeypatch.setattr(provision, "load_config", lambda: config)


def test_a_license_already_in_the_chain_is_kept_not_doubled(wire, monkeypatch, capsys):
    with_chain(monkeypatch, "SPB", "STANDARDPACK")
    client = wire(FakeGraph(licensed_user("sku-e1"), skus=SKUS))

    assert provision.main(["reuse", "--yes"]) == 0

    assert not [call for call in client.writes if call[0] == "assign_license"]
    assert "license kept — the account already holds STANDARDPACK" in capsys.readouterr().out


def test_a_license_outside_the_chain_is_reported_not_stacked(wire, monkeypatch, capsys):
    with_chain(monkeypatch, "SPB")
    client = wire(FakeGraph(licensed_user("sku-visio"), skus=SKUS))

    assert provision.main(["reuse", "--yes"]) == 1

    assert not [call for call in client.writes if call[0] == "assign_license"]
    err = capsys.readouterr().err
    assert "already holds VISIOCLIENT, which isn't in the licensing.default chain" in err


def test_an_unlicensed_account_gets_the_chain_pick(wire, monkeypatch):
    with_chain(monkeypatch, "SPB")
    client = wire(FakeGraph(licensed_user(), skus=SKUS))

    assert provision.main(["reuse", "--yes"]) == 0

    assert ("assign_license", USER_ID, "sku-spb") in client.writes


def test_flat_license_sku_is_kept_when_already_held(wire, monkeypatch, capsys):
    monkeypatch.setattr(provision, "load_config", lambda: dict(CONFIG, license_sku="sku-spb"))
    client = wire(FakeGraph(licensed_user("SKU-SPB")))

    assert provision.main(["reuse", "--yes"]) == 0

    assert not [call for call in client.writes if call[0] == "assign_license"]
    assert "license kept" in capsys.readouterr().out
