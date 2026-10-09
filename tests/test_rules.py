"""Tests for the rules that decide who gets what: groups, licenses, names.

These are the pure functions in provision.py that turn hire.yaml plus
config.yaml into a display name, a group list and a license choice, plus
provision_extras, which applies them. Everything runs against small config
dicts and a recording FakeGraph — no tenant, no network.

Run from the repo root:  python -m pytest -q
"""

import pytest

import provision
from graph_api import GraphError
from provision import (
    choose_license, display_name_for, display_title, enrich_from_property,
    groups_for, is_corporate, property_label, property_numbers,
)

G = lambda n: f"00000000-0000-0000-0000-{n:012d}"   # noqa: E731 — readable GUIDs

CONFIG = {
    "tenant": {"domain": "example.com"},
    "corporate_property": "50",
    "naming": {"title_display": {"Concierge/Leasing": "Leasing Concierge"}},
    "groups": {
        "corporate": [G(3)],
        "site": [G(4)],
        "titles": {
            "Property Manager": [G(5), G(1)],   # G(1) is also a property group
            "Maintenance Technician": [G(6)],
        },
    },
    "joined_properties": {"720/721": {"name": "Twin Oaks"}},
    "properties": {
        "101": {"name": "Example Apartments", "rpm": "rpm-a@example.com", "groups": [G(1), G(2)]},
        "720": {"name": "Oak North", "rpm": "rpm-b@example.com", "groups": [G(7)]},
        "721": {"name": "Oak South", "rpm": "rpm-b@example.com", "groups": [G(8)]},
        "730": {"name": "Pine", "rpm": "rpm-c@example.com"},
    },
}


# --- property numbers --------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("619", ["619"]),
    (619, ["619"]),
    ("720/721", ["720", "721"]),
    (" 720 / 721 ", ["720", "721"]),
    ("720 & 721", ["720", "721"]),
    ("", []),
    (None, []),
])
def test_property_numbers(value, expected):
    assert property_numbers(value) == expected


def test_property_label_normalizes_a_pair():
    assert property_label(" 720 / 721") == "720/721"
    assert property_label("619") == "619"


def test_is_corporate_compares_to_the_configured_property():
    assert is_corporate({"property_number": "50"}, CONFIG)
    assert is_corporate({"property_number": 50}, {})            # default "50"
    assert not is_corporate({"property_number": "101"}, CONFIG)
    assert is_corporate({"property_number": "9"}, {"corporate_property": 9})
    assert not is_corporate({"property_number": "50/51"}, CONFIG)   # a pair is never corporate


# --- filling hire.yaml in from config.yaml ---------------------------------------

def test_property_name_and_rpm_come_from_the_property_entry():
    hire = enrich_from_property({"property_number": "101"}, CONFIG)
    assert hire["property_name"] == "Example Apartments"
    assert hire["rpm_email"] == "rpm-a@example.com"


def test_explicit_values_in_hire_yaml_win():
    hire = enrich_from_property(
        {"property_number": "101", "property_name": "The Annex", "rpm_email": "me@example.com"}, CONFIG,
    )
    assert (hire["property_name"], hire["rpm_email"]) == ("The Annex", "me@example.com")


def test_a_joined_pair_takes_its_name_from_joined_properties_and_dedupes_rpms():
    hire = enrich_from_property({"property_number": "720/721"}, CONFIG)
    assert hire["property_name"] == "Twin Oaks"
    assert hire["rpm_email"] == "rpm-b@example.com"   # same RPM twice -> once


def test_a_joined_pair_without_an_entry_joins_the_names_and_ccs_every_rpm():
    hire = enrich_from_property({"property_number": "101/730"}, CONFIG)
    assert hire["property_name"] == "Example Apartments & Pine"
    assert hire["rpm_email"] == "rpm-a@example.com; rpm-c@example.com"


def test_joined_pair_lookup_accepts_either_order():
    assert enrich_from_property({"property_number": "721/720"}, CONFIG)["property_name"] == "Twin Oaks"


def test_an_unknown_property_leaves_the_hire_untouched():
    hire = {"property_number": "999"}
    assert enrich_from_property(hire, CONFIG) == hire


# --- names --------------------------------------------------------------------------

def test_display_title_restates_awkward_form_wording_case_insensitively():
    assert display_title({"title": "concierge/leasing"}, CONFIG) == "Leasing Concierge"
    assert display_title({"title": "Property Manager"}, CONFIG) == "Property Manager"
    assert display_title({"title": " Leasing "}, {}) == "Leasing"


def test_property_accounts_display_as_title_at_property():
    hire = {"first_name": "Taylor", "last_name": "Example", "title": "Concierge/Leasing",
            "property_number": "101", "property_name": "Example Apartments"}
    assert display_name_for(hire, CONFIG) == "Leasing Concierge at Example Apartments"


def test_corporate_accounts_keep_a_personal_name():
    hire = {"first_name": "Taylor", "last_name": "Example", "title": "Controller",
            "property_number": "50", "property_name": "Head Office"}
    assert display_name_for(hire, CONFIG) == "Taylor Example"


@pytest.mark.parametrize("missing", ["title", "property_name", "property_number"])
def test_a_missing_piece_falls_back_to_the_personal_name(missing):
    hire = {"first_name": "Taylor", "last_name": "Example", "title": "Manager",
            "property_number": "101", "property_name": "Example Apartments"}
    del hire[missing]
    assert display_name_for(hire, CONFIG) == "Taylor Example"


# --- groups -------------------------------------------------------------------------

def test_groups_merge_property_site_and_title_without_duplicates():
    hire = {"property_number": "101", "title": "property manager"}   # case differs from config
    assert groups_for(hire, CONFIG) == [G(1), G(2), G(4), G(5)]   # G(1) listed once, order kept


def test_corporate_hires_get_the_corporate_set_not_the_site_set():
    assert groups_for({"property_number": "50", "title": "Controller"}, CONFIG) == [G(3)]


def test_a_joined_pair_joins_both_properties_groups():
    hire = {"property_number": "720/721", "title": "Maintenance Technician"}
    assert groups_for(hire, CONFIG) == [G(7), G(8), G(4), G(6)]


def test_no_property_means_only_title_groups():
    assert groups_for({"title": "Maintenance Technician"}, CONFIG) == [G(6)]
    assert groups_for({}, CONFIG) == []


def test_a_title_match_is_exact_after_trimming_and_case():
    assert groups_for({"title": "  PROPERTY MANAGER "}, CONFIG) == [G(5), G(1)]
    assert groups_for({"title": "Property Manager II"}, CONFIG) == []


# --- licenses -------------------------------------------------------------------------

def sku(part, sku_id, free):
    return {"skuId": sku_id, "skuPartNumber": part, "prepaidUnits": {"enabled": 10}, "consumedUnits": 10 - free}


class SkuClient:
    def __init__(self, skus=(), error=None):
        self.skus, self.error = list(skus), error

    def list_skus(self):
        if self.error:
            raise self.error
        return list(self.skus)


LICENSING = {
    "corporate": ["SPB"],
    "maintenance": ["STANDARDPACK", "EXCHANGESTANDARD", "SPB"],
    "default": ["SPB", "STANDARDPACK"],
}


def test_without_rules_the_flat_sku_is_used_or_the_step_is_skipped():
    assert choose_license(SkuClient(), {"license_sku": "sku-1"}, {}) == ("sku-1", "sku-1", [], None)
    sku_id, label, notes, problem = choose_license(SkuClient(), {}, {})
    assert sku_id is None and problem is None and "license skipped" in notes[0]


def test_the_chain_is_picked_by_who_the_hire_is():
    client = SkuClient([sku("SPB", "s-spb", 3), sku("STANDARDPACK", "s-e1", 3), sku("EXCHANGESTANDARD", "s-ex", 3)])
    config = {"licensing": LICENSING, "corporate_property": "50"}
    assert choose_license(client, config, {"property_number": "50"})[0] == "s-spb"
    assert choose_license(client, config, {"property_number": "101", "title": "Maintenance Tech"})[0] == "s-e1"
    assert choose_license(client, config, {"property_number": "101", "title": "Leasing"})[0] == "s-spb"
    assert "corporate rule" in choose_license(client, config, {"property_number": "50"})[1]


def test_the_first_sku_with_free_seats_wins():
    client = SkuClient([sku("STANDARDPACK", "s-e1", 0), sku("EXCHANGESTANDARD", "s-ex", 2), sku("SPB", "s-spb", 5)])
    sku_id, label, notes, problem = choose_license(client, {"licensing": LICENSING}, {"title": "Maintenance"})
    assert sku_id == "s-ex" and problem is None
    assert notes == ["no STANDARDPACK seats free — trying next option"]
    assert label == "EXCHANGESTANDARD (maintenance rule, 2 seat(s) free)"


def test_no_seats_anywhere_is_a_problem_not_a_silent_skip():
    client = SkuClient([sku("SPB", "s-spb", 0), sku("STANDARDPACK", "s-e1", 0)])
    sku_id, _, notes, problem = choose_license(client, {"licensing": LICENSING}, {"title": "Leasing"})
    assert sku_id is None
    assert "no seats free on any licensing.default option (SPB, STANDARDPACK)" in problem


def test_an_entry_not_in_the_tenant_is_noted_and_skipped():
    client = SkuClient([sku("STANDARDPACK", "s-e1", 1)])
    sku_id, _, notes, _ = choose_license(client, {"licensing": LICENSING}, {"title": "Leasing"})
    assert sku_id == "s-e1"
    assert notes == ["license option SPB not in this tenant — skipping it"]


def test_entries_may_be_sku_ids_and_match_case_insensitively():
    client = SkuClient([sku("SPB", "S-SPB", 1)])
    assert choose_license(client, {"licensing": {"default": ["s-spb"]}}, {})[0] == "S-SPB"


def test_maintenance_keywords_are_configurable():
    client = SkuClient([sku("STANDARDPACK", "s-e1", 1), sku("SPB", "s-spb", 1)])
    config = {"licensing": dict(LICENSING, maintenance_keywords=["engineer"])}
    assert choose_license(client, config, {"title": "Site Engineer"})[0] == "s-e1"
    assert choose_license(client, config, {"title": "Maintenance Technician"})[0] == "s-spb"


def test_a_missing_chain_skips_with_a_note():
    sku_id, _, notes, problem = choose_license(SkuClient(), {"licensing": {"default": ["SPB"]}}, {"property_number": "50"})
    assert sku_id is None and problem is None
    assert "no licensing.corporate chain" in notes[0]


def test_a_403_on_the_sku_list_names_the_permission():
    client = SkuClient(error=GraphError("denied", status=403))
    _, _, _, problem = choose_license(client, {"licensing": LICENSING}, {})
    assert "LicenseAssignment.Read.All" in problem


# --- applying it: provision_extras ---------------------------------------------------

STAFF = {"id": G(1), "displayName": "All Staff", "groupTypes": [], "mailEnabled": False}
TEAM = {"id": G(2), "displayName": "Site Team", "groupTypes": ["Unified"], "mailEnabled": True, "mail": "team@example.com"}
DL = {"id": G(4), "displayName": "Site DL", "groupTypes": [], "mailEnabled": True, "mail": "site@example.com"}


class FakeGraph:
    def __init__(self, groups, skus=(), already_member=(), license_error=None):
        self.groups = {g["id"]: g for g in groups}
        self.skus = list(skus)
        self.already_member = set(already_member)
        self.license_error = license_error
        self.writes = []

    def list_skus(self):
        return list(self.skus)

    def assign_license(self, user_id, sku_id):
        self.writes.append(("assign_license", user_id, sku_id))
        if self.license_error:
            raise self.license_error

    def get_group(self, group_id, select="displayName"):
        return self.groups.get(group_id)

    def add_group_member(self, group_id, user_id):
        self.writes.append(("add_group_member", group_id, user_id))
        if group_id in self.already_member:
            raise GraphError(
                "Graph API error (400) — Request_BadRequest: One or more added object "
                "references already exist for the following modified properties: 'members'.",
                status=400,
            )


@pytest.fixture
def quiet_log(monkeypatch, tmp_path):
    monkeypatch.setattr(provision, "LOG_DIR", tmp_path / "logs")


def test_extras_assign_the_license_and_join_each_kind_of_group_appropriately(quiet_log, capsys):
    client = FakeGraph([STAFF, TEAM, DL], skus=[sku("SPB", "s-spb", 1)])
    config = {"licensing": {"default": ["SPB"]}, "groups": {"site": [G(1), G(2), G(4)]}}

    issues = provision.provision_extras(client, config, {"property_number": "101"}, "u1", dry=False, upn="t@example.com")

    assert issues == []
    # The M365 group (Unified) is joined through Graph like a security
    # group; the classic DL is printed, since Graph can't write it.
    assert client.writes == [("assign_license", "u1", "s-spb"), ("add_group_member", G(1), "u1"), ("add_group_member", G(2), "u1")]
    out = capsys.readouterr().out
    assert "license assigned: SPB (default rule, 1 seat(s) free)" in out
    assert "added to group: All Staff" in out and "added to group: Site Team" in out
    assert "Add-DistributionGroupMember -Identity 'site@example.com' -Member 't@example.com'  # Site DL" in out


def test_already_a_member_counts_as_success(quiet_log, capsys):
    client = FakeGraph([STAFF], already_member=[G(1)])

    issues = provision.provision_extras(client, {"groups": {"site": [G(1)]}}, {"property_number": "101"}, "u1", dry=False)

    assert issues == []
    assert "already in group: All Staff" in capsys.readouterr().out


def test_a_group_missing_from_the_tenant_is_an_issue_and_the_rest_continue(quiet_log):
    client = FakeGraph([STAFF])

    issues = provision.provision_extras(client, {"groups": {"site": [G(9), G(1)]}}, {"property_number": "101"}, "u1", dry=False)

    assert issues == [f"group {G(9)} not found — check config.yaml"]
    assert ("add_group_member", G(1), "u1") in client.writes


def test_dry_run_reads_the_real_groups_but_writes_nothing(quiet_log, capsys):
    client = FakeGraph([STAFF, DL], skus=[sku("SPB", "s-spb", 1)])
    config = {"licensing": {"default": ["SPB"]}, "groups": {"site": [G(1), G(4)]}}

    issues = provision.provision_extras(client, config, {"property_number": "101"}, "u1", dry=True, upn="t@example.com")

    assert issues == [] and client.writes == []
    out = capsys.readouterr().out
    assert "[dry-run] would assign license SPB" in out
    assert "[dry-run] would add to group: All Staff" in out
    assert "Add-DistributionGroupMember" in out   # the paste-ready step is shown either way


def test_the_last_seat_vanishing_mid_run_is_explained(quiet_log, capsys):
    taken = GraphError("Graph API error (400) — Request_BadRequest: Subscription ... does not have any available licenses.", status=400)
    client = FakeGraph([], skus=[sku("SPB", "s-spb", 1)], license_error=taken)

    issues = provision.provision_extras(client, {"licensing": {"default": ["SPB"]}}, {}, "u1", dry=False)

    assert issues == ["license not assigned — the last free seat was taken mid-run"]


def test_nothing_mapped_is_a_note_not_an_issue(quiet_log, capsys):
    issues = provision.provision_extras(FakeGraph([]), {}, {"title": "Nobody"}, "u1", dry=False)

    assert issues == []
    out = capsys.readouterr().out
    assert "license skipped" in out and "groups skipped" in out
