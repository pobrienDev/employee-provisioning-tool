"""Tests for the offboarding flow (provision.cmd_terminate).

These run with zero network calls and touch no tenant: Microsoft Graph is
replaced by FakeGraph, an in-memory stand-in that records every call in order,
so a test can assert not just *what* the tool did but the sequence it did it
in. That ordering is the point of the command: lock the account out first,
then clean up.

config.yaml, .env, and the logs directory are all replaced per test, so
nothing here reads or writes real project files.

Run from the repo root:  python -m pytest -q
"""

import pytest

import provision
from graph_api import GraphError
from provision import ProvisionError, group_kind

DOMAIN = "example.com"
UPN = f"manager619@{DOMAIN}"
USER_ID = "user-1"

STAFF = {"id": "g-staff", "displayName": "Elm Court Staff", "groupTypes": [], "mailEnabled": False}
MANAGERS = {"id": "g-mgrs", "displayName": "Site Managers", "groupTypes": [], "mailEnabled": False}
TEAM = {"id": "g-team", "displayName": "Elm Court Team", "groupTypes": ["Unified"], "mailEnabled": True}
ALL_STAFF_DL = {
    "id": "g-dl", "displayName": "All Staff", "groupTypes": [],
    "mailEnabled": True, "mail": "allstaff@example.com",
}
DYNAMIC = {"id": "g-dyn", "displayName": "All Licensed Users", "groupTypes": ["DynamicMembership"]}


class FakeGraph:
    """In-memory stand-in for graph_api.GraphClient that records every call.

      .calls     every method call as a tuple, in order, e.g.
                 ("update_user", "user-1", {"accountEnabled": False})
      .writes    just the calls that would change the tenant
      fail_group a group id whose removal raises GraphError, to exercise
                 the keep-going-and-report path
      roles      directory roles get_member_roles hands back
    """

    WRITES = {"update_user", "revoke_sessions", "remove_group_member", "remove_licenses"}

    def __init__(self, user=None, groups=(), fail_group=None, roles=()):
        self.user = user
        self.groups = list(groups)
        self.fail_group = fail_group
        self.roles = list(roles)
        self.calls = []

    @property
    def writes(self):
        return [call for call in self.calls if call[0] in self.WRITES]

    def get_user(self, upn_or_id, select):
        self.calls.append(("get_user", upn_or_id))
        return self.user

    def get_member_groups(self, user_id):
        self.calls.append(("get_member_groups", user_id))
        return list(self.groups)

    def get_member_roles(self, user_id):
        self.calls.append(("get_member_roles", user_id))
        return list(self.roles)

    def update_user(self, user_id, changes):
        self.calls.append(("update_user", user_id, changes))

    def revoke_sessions(self, user_id):
        self.calls.append(("revoke_sessions", user_id))

    def remove_group_member(self, group_id, user_id):
        self.calls.append(("remove_group_member", group_id, user_id))
        if group_id == self.fail_group:
            raise GraphError("Graph API error (403) — Authorization_RequestDenied", status=403)

    def remove_licenses(self, user_id, sku_ids):
        self.calls.append(("remove_licenses", user_id, list(sku_ids)))


def make_user(licenses=("sku-business-premium",), inherited=()):
    """An account holding `licenses` directly and `inherited` through a
    group; licenseAssignmentStates is only present when a group-assigned
    license is involved, which also covers callers that never asked for it."""
    user = {
        "id": USER_ID,
        "displayName": "Property Manager at Elm Court",
        "userPrincipalName": UPN,
        "accountEnabled": True,
        "jobTitle": "Property Manager",
        "assignedLicenses": [{"skuId": sku} for sku in (*licenses, *inherited)],
    }
    if inherited:
        user["licenseAssignmentStates"] = (
            [{"skuId": sku, "assignedByGroup": None, "state": "Active"} for sku in licenses]
            + [{"skuId": sku, "assignedByGroup": "g-licensing", "state": "Active"} for sku in inherited]
        )
    return user


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Point provision at a fake client, a fake config, and a temp log dir.

    Returns a function: wire(client) installs that client and hands it back.
    """
    monkeypatch.setattr(provision, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(provision, "load_config", lambda: {"tenant": {"domain": DOMAIN}})

    def install(client):
        monkeypatch.setattr(provision.GraphClient, "from_env", classmethod(lambda cls: client))
        return client

    return install


def audit_text(tmp_path):
    logs = list((tmp_path / "logs").glob("provision-*.log"))
    assert len(logs) == 1
    return logs[0].read_text(encoding="utf-8")


# --- group_kind: which memberships Graph can end -----------------------------

@pytest.mark.parametrize("group, expected", [
    (STAFF, "graph"),             # plain security group
    (TEAM, "graph"),              # Microsoft 365 group: mail-enabled but Unified
    (ALL_STAFF_DL, "exchange"),   # distribution list: mail-enabled, not Unified
    (DYNAMIC, "dynamic"),         # membership follows attributes
    ({"id": "g", "groupTypes": None}, "graph"),   # Graph can return null here
])
def test_group_kind(group, expected):
    assert group_kind(group) == expected


def test_dynamic_wins_over_mail_enabled():
    group = {"id": "g", "groupTypes": ["DynamicMembership"], "mailEnabled": True}
    assert group_kind(group) == "dynamic"


# --- preview and dry-run: nothing may change ---------------------------------

def test_without_yes_nothing_is_written(wire, capsys):
    client = wire(FakeGraph(make_user(), [STAFF, MANAGERS]))

    assert provision.main(["terminate", "manager619"]) == 1

    assert client.writes == []
    captured = capsys.readouterr()
    assert "plan: disable account, revoke sessions" in captured.out
    assert "re-run with --yes" in captured.err


def test_dry_run_writes_nothing_and_says_what_it_would_do(wire, capsys, tmp_path):
    client = wire(FakeGraph(make_user(), [STAFF, MANAGERS]))

    assert provision.main(["terminate", "manager619", "--dry-run"]) == 0

    assert client.writes == []
    out = capsys.readouterr().out
    assert "[dry-run] would disable the account and revoke every session" in out
    assert "[dry-run] would remove from group: Elm Court Staff" in out
    assert "[dry-run] would remove from group: Site Managers" in out
    assert "[dry-run] would remove 1 license(s)" in out
    assert "(dry-run)" in audit_text(tmp_path)


def test_dry_run_wins_even_with_yes(wire):
    client = wire(FakeGraph(make_user(), [STAFF]))

    assert provision.main(["terminate", "manager619", "--yes", "--dry-run"]) == 0

    assert client.writes == []


# --- the real thing: lockout first, then clean up ----------------------------

def test_offboarding_locks_out_before_any_cleanup(wire):
    client = wire(FakeGraph(make_user(), [STAFF, MANAGERS]))

    assert provision.main(["terminate", "manager619", "--yes"]) == 0

    assert client.writes == [
        ("update_user", USER_ID, {"accountEnabled": False}),
        ("revoke_sessions", USER_ID),
        ("remove_group_member", "g-staff", USER_ID),
        ("remove_group_member", "g-mgrs", USER_ID),
        ("remove_licenses", USER_ID, ["sku-business-premium"]),
    ]


def test_only_graph_managed_groups_are_removed(wire, capsys):
    client = wire(FakeGraph(make_user(), [STAFF, TEAM, ALL_STAFF_DL, DYNAMIC]))

    # Exit 1: the distribution list removal is still open after the run.
    assert provision.main(["terminate", "manager619", "--yes"]) == 1

    removed = [call[1] for call in client.writes if call[0] == "remove_group_member"]
    assert removed == ["g-staff", "g-team"]
    captured = capsys.readouterr()
    # The distribution list comes back as a paste-ready Exchange command...
    assert (
        "Remove-DistributionGroupMember -Identity 'allstaff@example.com' "
        f"-Member '{UPN}' -Confirm:$false"
    ) in captured.out
    # ...counted as work that remains, not as a finished offboarding...
    assert "1 distribution list removal(s) still to paste" in captured.err
    # ...and the dynamic group is explained rather than touched.
    assert "All Licensed Users is a dynamic group" in captured.out


def test_directory_roles_are_reported_as_open_follow_ups(wire, capsys, tmp_path):
    client = wire(FakeGraph(
        make_user(), [STAFF], roles=[{"id": "role-1", "displayName": "User Administrator"}],
    ))

    assert provision.main(["terminate", "manager619", "--yes"]) == 1

    # The lockout and the group removal still happen; the role is not
    # touched (the app holds no role permission) but it is never silent —
    # a reused account would hand that role to the next hire.
    assert [call[0] for call in client.writes] == [
        "update_user", "revoke_sessions", "remove_group_member", "remove_licenses",
    ]
    captured = capsys.readouterr()
    assert "1 directory role(s) to remove by hand" in captured.out
    assert "directory role User Administrator stays assigned" in captured.err
    assert "directory role User Administrator stays assigned" in audit_text(tmp_path)


def test_dry_run_lists_directory_roles_too(wire, capsys):
    client = wire(FakeGraph(make_user(), [], roles=[{"id": "role-1", "displayName": None}]))

    assert provision.main(["terminate", "manager619", "--dry-run"]) == 0

    assert client.writes == []
    # Without a role-reading permission Graph returns only the id.
    assert "directory role role-1 stays assigned" in capsys.readouterr().out


def test_account_without_licenses_skips_license_removal(wire, capsys):
    client = wire(FakeGraph(make_user(licenses=()), [STAFF]))

    assert provision.main(["terminate", "manager619", "--yes"]) == 0

    assert not [call for call in client.writes if call[0] == "remove_licenses"]
    assert "no licenses to remove" in capsys.readouterr().out


def test_every_action_lands_in_the_audit_log(wire, tmp_path):
    wire(FakeGraph(make_user(), [STAFF]))

    provision.main(["terminate", "manager619", "--yes"])

    log = audit_text(tmp_path)
    assert f"terminate: {UPN}" in log
    assert "account disabled" in log and "sessions revoked" in log
    # Every line names who ran it.
    assert all(f"[{provision.OPERATOR}]" in line for line in log.splitlines())
    assert "@" in provision.OPERATOR
    assert "removed from group: Elm Court Staff" in log
    assert "removed 1 license(s)" in log


# --- failure handling ---------------------------------------------------------

def test_unknown_user_changes_nothing(wire, capsys):
    client = wire(FakeGraph(user=None))

    assert provision.main(["terminate", "nobody", "--yes"]) == 1

    assert client.writes == []
    assert f"nobody@{DOMAIN} not found" in capsys.readouterr().err


def test_a_failed_group_removal_does_not_stop_the_offboarding(wire, capsys, tmp_path):
    client = wire(FakeGraph(make_user(), [STAFF, MANAGERS], fail_group="g-staff"))

    # Exit code 1: the operator must see that something needs a second look.
    assert provision.main(["terminate", "manager619", "--yes"]) == 1

    # The account was still locked out first, the other group still left,
    # and the licenses were still reclaimed.
    assert client.writes[:2] == [
        ("update_user", USER_ID, {"accountEnabled": False}),
        ("revoke_sessions", USER_ID),
    ]
    assert ("remove_group_member", "g-mgrs", USER_ID) in client.writes
    assert ("remove_licenses", USER_ID, ["sku-business-premium"]) in client.writes
    err = capsys.readouterr().err
    assert "offboarding finished with issues" in err
    assert "could not remove from Elm Court Staff" in err
    assert "could not remove from Elm Court Staff" in audit_text(tmp_path)


def test_cmd_terminate_raises_so_callers_can_tell(wire):
    wire(FakeGraph(user=None))
    args = provision.argparse.Namespace(upn="nobody", yes=True, dry_run=False, convert_shared=False)

    with pytest.raises(ProvisionError, match="not found"):
        provision.cmd_terminate(args)


# --- UPN handling -------------------------------------------------------------

def test_bare_username_gets_the_tenant_domain(wire):
    client = wire(FakeGraph(make_user(), []))

    provision.main(["terminate", "manager619", "--dry-run"])

    assert ("get_user", UPN) in client.calls


def test_full_upn_is_used_as_given(wire):
    client = wire(FakeGraph(make_user(), []))

    provision.main(["terminate", "someone@other.example", "--dry-run"])

    assert ("get_user", "someone@other.example") in client.calls


# --- --convert-shared: keep the mail flowing ----------------------------------

def test_convert_shared_keeps_memberships_and_converts_before_unlicensing(wire, monkeypatch):
    client = wire(FakeGraph(make_user(), [STAFF, ALL_STAFF_DL]))
    monkeypatch.setattr(
        provision, "convert_mailbox_shared",
        lambda upn: client.calls.append(("convert_mailbox_shared", upn)),
    )

    assert provision.main(["terminate", "manager619", "--yes", "--convert-shared"]) == 0

    steps = [
        call[0] for call in client.calls
        if call[0] in client.WRITES or call[0] == "convert_mailbox_shared"
    ]
    # Lock out, convert while the mailbox is still licensed, then unlicense —
    # and no memberships are removed, so group and list mail keeps arriving.
    assert steps == ["update_user", "revoke_sessions", "convert_mailbox_shared", "remove_licenses"]


def test_failed_conversion_keeps_the_licenses(wire, monkeypatch, capsys):
    client = wire(FakeGraph(make_user(), [STAFF]))

    def boom(upn):
        raise ProvisionError("mailbox conversion failed: ExchangeOnlineManagement module not found")

    monkeypatch.setattr(provision, "convert_mailbox_shared", boom)

    assert provision.main(["terminate", "manager619", "--yes", "--convert-shared"]) == 1

    # Removing the license from an unconverted mailbox starts its deletion
    # clock, so the tool must leave it in place.
    assert not [call for call in client.writes if call[0] == "remove_licenses"]
    # The lockout is not rolled back: a terminated employee stays locked out.
    assert client.writes[:2] == [
        ("update_user", USER_ID, {"accountEnabled": False}),
        ("revoke_sessions", USER_ID),
    ]
    assert "licenses kept" in capsys.readouterr().err


# --- which tenant -------------------------------------------------------------

def test_every_write_command_announces_its_tenant_first(wire, capsys, tmp_path):
    client = wire(FakeGraph(make_user(), []))
    client.tenant_id = "tenant-1"

    provision.main(["terminate", "manager619", "--dry-run"])

    out = capsys.readouterr().out
    assert out.index("tenant: tenant-1 (example.com)") < out.index("Terminating:")
    assert "tenant: tenant-1 (example.com)" in audit_text(tmp_path)


# --- every write is logged as it lands -----------------------------------------

def test_a_failed_revocation_still_logs_the_disable(wire, capsys, tmp_path):
    class RevokeFails(FakeGraph):
        def revoke_sessions(self, user_id):
            super().revoke_sessions(user_id)
            raise GraphError("Graph API error (503) — ServiceUnavailable", status=503)

    client = wire(RevokeFails(make_user(), [STAFF]))

    assert provision.main(["terminate", "manager619", "--yes"]) == 1

    assert client.writes == [
        ("update_user", USER_ID, {"accountEnabled": False}),
        ("revoke_sessions", USER_ID),
    ]
    log = audit_text(tmp_path)
    # The disable happened and the log must say so, ahead of the error.
    assert log.index("account disabled") < log.index("error (terminate)")
    assert "sessions revoked" not in log


# --- group-assigned licenses ------------------------------------------------------

def test_group_assigned_licenses_are_not_removed_from_the_user(wire, capsys):
    client = wire(FakeGraph(make_user(licenses=("sku-direct",), inherited=("sku-via-group",)), [STAFF]))

    assert provision.main(["terminate", "manager619", "--yes"]) == 0

    # Only the direct SKU goes in the call — an inherited one would make
    # the whole assignLicense request fail.
    assert ("remove_licenses", USER_ID, ["sku-direct"]) in client.writes
    out = capsys.readouterr().out
    assert "remove 1 license(s) (1 group-assigned follow the memberships)" in out
    assert "1 group-assigned license(s) not removed directly" in out


def test_an_account_with_only_group_licenses_has_nothing_to_remove(wire, capsys):
    client = wire(FakeGraph(make_user(licenses=(), inherited=("sku-via-group",)), []))

    assert provision.main(["terminate", "manager619", "--yes"]) == 0

    assert not [call for call in client.writes if call[0] == "remove_licenses"]
    assert "no licenses to remove" in capsys.readouterr().out


def test_a_failed_license_removal_is_an_issue_not_a_crash(wire, capsys, tmp_path):
    class RemoveFails(FakeGraph):
        def remove_licenses(self, user_id, sku_ids):
            super().remove_licenses(user_id, sku_ids)
            raise GraphError("Graph API error (400) — Request_BadRequest: License assignment failed", status=400)

    client = wire(RemoveFails(make_user(), [STAFF]))

    assert provision.main(["terminate", "manager619", "--yes"]) == 1

    assert ("remove_group_member", "g-staff", USER_ID) in client.writes
    err = capsys.readouterr().err
    assert "offboarding finished with issues" in err
    assert "could not remove the license(s)" in err
    assert "could not remove the license(s)" in audit_text(tmp_path)


def test_split_licenses_without_assignment_states_treats_all_as_direct():
    assert provision.split_licenses({"assignedLicenses": [{"skuId": "a"}, {"skuId": "b"}]}) == (["a", "b"], [])


def test_split_licenses_keeps_a_sku_that_is_both_direct_and_inherited_as_direct():
    user = {
        "assignedLicenses": [{"skuId": "a"}],
        "licenseAssignmentStates": [
            {"skuId": "a", "assignedByGroup": None},
            {"skuId": "a", "assignedByGroup": "g1"},
        ],
    }
    assert provision.split_licenses(user) == (["a"], [])
