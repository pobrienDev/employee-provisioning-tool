"""Tests for collision-safe UPN generation (provision.pick_upn).

These run with zero network calls: Microsoft Graph is replaced by FakeGraph,
an in-memory stand-in that answers get_user() from a dictionary and records
every UPN it was asked about, so a test can assert the exact sequence of
candidates the ladder tried. No credentials, no tenant, no .env required.

Run from the repo root:  python -m pytest -q
"""

import pytest

from graph_api import GraphError
from provision import ProvisionError, pick_upn, sanitize_local

CONFIG = {"tenant": {"domain": "d.com"}}


class FakeGraph:
    """In-memory stand-in for graph_api.GraphClient.

    pick_upn only ever calls get_user(upn, select) and only cares whether the
    answer is None (free) or a dict (taken), so that's all the fake provides —
    plus a record of every UPN it was asked about, so a test can assert the
    exact order the ladder tried them in.

      FakeGraph(existing)      existing maps a UPN -> the display name of an
                               account that already exists
      FakeGraph(aliases=...)   aliases maps an address -> who already
                               receives mail there (an alias, a group...)
      .get_user(upn, select)   records upn in .attempted, then answers like
                               the real client: a dict with "id" and
                               "displayName" when the UPN exists, else None
      .address_holder(local, domain)  answers from aliases, recording the
                               address in .alias_checks
      .attempted               every UPN queried, in call order
    """

    def __init__(self, existing=None, aliases=None):
        self.existing = dict(existing or {})
        self.aliases = dict(aliases or {})
        self.attempted = []
        self.alias_checks = []

    def get_user(self, upn, select):
        # `select` is part of the real client's signature; the fake honors the
        # interface but has no fields to narrow.
        self.attempted.append(upn)
        if upn in self.existing:
            return {"id": f"id-{len(self.attempted)}", "displayName": self.existing[upn]}
        return None

    def address_holder(self, local, domain):
        address = f"{local}@{domain}"
        self.alias_checks.append(address)
        return self.aliases.get(address)


def hire(first, last):
    return {"first_name": first, "last_name": last}


def test_sanitizes_spaces_punctuation_and_accents():
    # The pure helper first...
    assert sanitize_local("José García") == "josegarcia"
    assert sanitize_local("O'Brien") == "obrien"
    assert sanitize_local("van Dyke") == "vandyke"
    # ...and the whole path: an accented name still yields an ASCII UPN.
    fake = FakeGraph({})
    assert pick_upn(fake, hire("José", "Muñoz"), CONFIG) == "jmunoz@d.com"


def test_first_initial_plus_last_name_when_available():
    fake = FakeGraph({})
    assert pick_upn(fake, hire("Pat", "Test"), CONFIG) == "ptest@d.com"
    # One lookup, nothing else tried.
    assert fake.attempted == ["ptest@d.com"]


def test_collision_advances_to_next_candidate():
    # jsmith@ already belongs to someone else, so John Smith should get the
    # next rung of the ladder: two letters of the first name + last name.
    fake = FakeGraph({"jsmith@d.com": "Jane Smith"})
    assert pick_upn(fake, hire("John", "Smith"), CONFIG) == "josmith@d.com"
    # The order proves the ladder, not just the outcome.
    assert fake.attempted == ["jsmith@d.com", "josmith@d.com"]


def test_two_collisions_advance_twice():
    fake = FakeGraph({"jsmith@d.com": "Jane Smith", "josmith@d.com": "Jo Smith"})
    assert pick_upn(fake, hire("John", "Smith"), CONFIG) == "johsmith@d.com"
    assert fake.attempted == ["jsmith@d.com", "josmith@d.com", "johsmith@d.com"]


def test_numbered_fallback_after_every_letter_variant_is_taken():
    # Al Bee has only two letter-based stems: abee, albee.
    fake = FakeGraph({"abee@d.com": "Alma Bee", "albee@d.com": "Alan Bee"})
    assert pick_upn(fake, hire("Al", "Bee"), CONFIG) == "albee2@d.com"
    assert fake.attempted == ["abee@d.com", "albee@d.com", "albee2@d.com"]


def test_exhausting_all_variants_raises():
    taken = {"abee@d.com": "x", "albee@d.com": "x"}
    taken.update({f"albee{n}@d.com": "x" for n in range(2, 10)})
    fake = FakeGraph(taken)
    with pytest.raises(ProvisionError):
        pick_upn(fake, hire("Al", "Bee"), CONFIG)
    # It tried every variant before giving up: 2 stems + numbered 2..9.
    assert len(fake.attempted) == 2 + 8


def test_missing_tenant_domain_raises_before_any_lookup():
    fake = FakeGraph({})
    with pytest.raises(ProvisionError):
        pick_upn(fake, hire("Pat", "Test"), {})
    assert fake.attempted == []


def test_unusable_name_raises_before_any_lookup():
    fake = FakeGraph({})
    with pytest.raises(ProvisionError):
        pick_upn(fake, hire("文", "字"), CONFIG)  # no ASCII letters survive
    assert fake.attempted == []


# --- addresses that aren't UPNs still count as taken ------------------------------

def test_an_email_alias_on_another_account_counts_as_taken():
    # The tool told someone to add tsmith@ as an alias on manager536@; a
    # later Tom Smith must not be given it as a "free" UPN.
    fake = FakeGraph(aliases={"tsmith@d.com": "Property Manager at Elm Court, manager536@d.com"})
    assert pick_upn(fake, hire("Tom", "Smith"), CONFIG) == "tosmith@d.com"
    assert fake.alias_checks == ["tsmith@d.com", "tosmith@d.com"]


def test_a_group_address_counts_as_taken():
    fake = FakeGraph(aliases={"sales@d.com": "Sales Team, sales@d.com"})
    assert pick_upn(fake, hire("S", "Ales"), CONFIG) == "sales2@d.com"


def test_the_upn_check_comes_first_and_skips_the_alias_check_for_taken_upns():
    fake = FakeGraph(existing={"jsmith@d.com": "Jane Smith"})
    assert pick_upn(fake, hire("John", "Smith"), CONFIG) == "josmith@d.com"
    # No alias lookup for a UPN that is already taken outright.
    assert fake.alias_checks == ["josmith@d.com"]


def test_a_failed_alias_check_warns_once_and_falls_back_to_the_upn_check(capsys):
    class AliasLookupBroken(FakeGraph):
        def address_holder(self, local, domain):
            raise GraphError("Graph API error (400) — Request_UnsupportedQuery", status=400)

    fake = AliasLookupBroken(existing={"jsmith@d.com": "Jane Smith"})
    assert pick_upn(fake, hire("John", "Smith"), CONFIG) == "josmith@d.com"
    out = capsys.readouterr().out
    assert out.count("warning: could not check whether") == 1
