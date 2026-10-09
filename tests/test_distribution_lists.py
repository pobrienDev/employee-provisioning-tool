"""Tests for the opt-in distribution list joins (provision.join_distribution_lists).

The real function shells out to Exchange Online PowerShell. Here
exchange_shell is replaced by FakeExchange, which never starts a process: it
captures the script it was handed and plays the part of the session by
writing per-list results to the temp file the script names, exactly where
the real session would. So these run offline, with no PowerShell installed.

The script is built from values that come out of config.yaml and the tenant,
so every value has to survive PowerShell's single-quote rules. An apostrophe
is legal in an email address (o'brien-team@example.com), and inside a
single-quoted PowerShell string it must be doubled.

Run from the repo root:  python -m pytest -q
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import provision

UPN = "tsmith@example.com"
PLAIN = ("allstaff@example.com", "All Staff")
APOSTROPHE = ("o'brien-team@example.com", "O'Brien Team")


def single_quoted_literals(script):
    """Decode every single-quoted PowerShell literal in a script.

    Inside '...', two apostrophes mean one literal apostrophe and nothing
    else is special. Raises if a literal is left open, which is what an
    unescaped apostrophe does to the rest of the script.
    """
    literals, i = [], 0
    while i < len(script):
        if script[i] != "'":
            i += 1
            continue
        i += 1
        chars = []
        while True:
            if i >= len(script):
                raise AssertionError("unterminated single-quoted string in generated PowerShell")
            if script[i] == "'":
                if script[i + 1 : i + 2] == "'":
                    chars.append("'")
                    i += 2
                    continue
                i += 1
                break
            chars.append(script[i])
            i += 1
        literals.append("".join(chars))
    return literals


class FakeExchange:
    """Stand-in for provision.exchange_shell.

      outcome    maps a list address -> "JOINED", or "FAILED <reason>";
                 addresses not listed produce no result line at all
      .script    the PowerShell body the tool generated
      .results_path  the temp file the script was told to write to
    """

    def __init__(self, outcome=None, returncode=0):
        self.outcome = outcome or {}
        self.returncode = returncode
        self.script = None
        self.results_path = None

    def __call__(self, body):
        self.script = body
        self.results_path = re.search(r"Add-Content -Path '((?:[^']|'')*)'", body).group(1).replace("''", "'")
        lines = [
            f"JOINED {address}" if result == "JOINED" else f"{result.split(' ', 1)[0]} {address} {result.split(' ', 1)[1]}"
            for address, result in self.outcome.items()
        ]
        Path(self.results_path).write_text("\n".join(lines), encoding="utf-8")
        return SimpleNamespace(returncode=self.returncode)


@pytest.fixture
def exchange(monkeypatch, tmp_path):
    monkeypatch.setattr(provision, "LOG_DIR", tmp_path / "logs")

    def install(fake):
        monkeypatch.setattr(provision, "exchange_shell", fake)
        return fake

    return install


# --- the bug: an apostrophe in a list address --------------------------------

def test_apostrophe_in_list_address_is_escaped_for_powershell(exchange):
    fake = exchange(FakeExchange({APOSTROPHE[0]: "JOINED"}))

    provision.join_distribution_lists(UPN, [APOSTROPHE])

    # Every literal must close; an unescaped apostrophe would leave one open
    # and turn the rest of the address into PowerShell code.
    literals = single_quoted_literals(fake.script)
    assert APOSTROPHE[0] in literals                    # -Identity decodes to the real address
    assert f"JOINED {APOSTROPHE[0]}" in literals        # and so does the result marker
    assert "-Identity 'o''brien-team@example.com'" in fake.script


def test_apostrophe_address_still_reports_as_joined(exchange, capsys):
    exchange(FakeExchange({APOSTROPHE[0]: "JOINED"}))

    issues = provision.join_distribution_lists(UPN, [APOSTROPHE])

    assert issues == []
    assert "added to distribution list: O'Brien Team" in capsys.readouterr().out


def test_apostrophe_in_upn_is_escaped_too(exchange):
    fake = exchange(FakeExchange({PLAIN[0]: "JOINED"}))

    provision.join_distribution_lists("d'angelo@example.com", [PLAIN])

    assert "d'angelo@example.com" in single_quoted_literals(fake.script)


def test_a_hostile_address_cannot_run_commands(exchange):
    # A value that tries to close the string and append a command must stay data.
    hostile = ("x'; Remove-Mailbox -Identity ceo@example.com; '", "Bad List")
    fake = exchange(FakeExchange())

    provision.join_distribution_lists(UPN, [hostile])

    assert hostile[0] in single_quoted_literals(fake.script)
    outside_strings = re.sub(r"'(?:[^']|'')*'", "''", fake.script)
    assert "Remove-Mailbox" not in outside_strings


# --- ordinary behavior ----------------------------------------------------------

def test_all_lists_go_through_one_exchange_session(exchange):
    calls = []
    fake = FakeExchange({PLAIN[0]: "JOINED", APOSTROPHE[0]: "JOINED"})

    def counting(body):
        calls.append(body)
        return fake(body)

    exchange(counting)

    assert provision.join_distribution_lists(UPN, [PLAIN, APOSTROPHE]) == []
    assert len(calls) == 1   # one sign-in prompt for the operator, not one per list


def test_a_failed_join_is_reported_with_a_paste_ready_fallback(exchange, capsys):
    exchange(FakeExchange({PLAIN[0]: "JOINED", APOSTROPHE[0]: "FAILED Couldn't find object"}))

    issues = provision.join_distribution_lists(UPN, [PLAIN, APOSTROPHE])

    assert issues == ["could not add to O'Brien Team: Couldn't find object"]
    out = capsys.readouterr().out
    assert "added to distribution list: All Staff" in out
    # Only the list that failed is offered for a manual retry, correctly escaped.
    assert "Add-DistributionGroupMember -Identity 'o''brien-team@example.com'" in out
    assert "Add-DistributionGroupMember -Identity 'allstaff@example.com'" not in out


def test_no_results_at_all_means_the_session_never_ran(exchange, capsys):
    exchange(FakeExchange(outcome={}, returncode=1))

    issues = provision.join_distribution_lists(UPN, [PLAIN])

    assert len(issues) == 1 and "distribution list joins failed" in issues[0]
    assert "exit code 1" in issues[0]
    # The hire can still be finished by hand.
    assert "Add-DistributionGroupMember -Identity 'allstaff@example.com'" in capsys.readouterr().out


def test_results_file_is_cleaned_up(exchange):
    fake = exchange(FakeExchange({PLAIN[0]: "JOINED"}))

    provision.join_distribution_lists(UPN, [PLAIN])

    assert not Path(fake.results_path).exists()


# --- the session never starting is no different from a failed join ----------------

GUID = "00000000-0000-0000-0000-000000000002"
DL_GROUP = {
    "id": GUID, "displayName": "All Staff", "groupTypes": [],
    "mailEnabled": True, "securityEnabled": False, "mail": PLAIN[0],
}


class ClientWithOneList:
    def list_skus(self):
        return []

    def get_group(self, group_id, select="displayName"):
        return DL_GROUP if group_id == GUID else None


@pytest.mark.parametrize("reason", [
    "no PowerShell found — Exchange steps need it",
    "Exchange Online PowerShell timed out — complete the sign-in prompt, or do the step manually in the admin center",
])
def test_when_the_exchange_session_cannot_run_the_paste_ready_commands_still_print(exchange, capsys, reason):
    def cannot_run(body):
        raise provision.ProvisionError(reason)

    exchange(cannot_run)
    config = {"groups": {"titles": {"Property Manager": [GUID]}}}

    issues = provision.provision_extras(
        ClientWithOneList(), config, {"title": "Property Manager"}, "user-1",
        dry=False, upn=UPN, join_dls=True,
    )

    assert issues == [reason]
    out = capsys.readouterr().out
    assert reason in out
    # The README's promise: whatever went wrong, the hire can be finished by hand.
    assert f"Add-DistributionGroupMember -Identity '{PLAIN[0]}' -Member '{UPN}'" in out


# --- one quoting rule for every value, typographic quotes included --------------------

CURLY = "\u2019"   # the right single quotation mark PowerShell also treats as a delimiter


@pytest.mark.parametrize("raw, literal", [
    ("plain@example.com", "'plain@example.com'"),
    ("o'brien@example.com", "'o''brien@example.com'"),
    (f"o{CURLY}brien@example.com", f"'o{CURLY}{CURLY}brien@example.com'"),
    ("\u2018a\u201ab\u201bc", "'\u2018\u2018a\u201a\u201ab\u201b\u201bc'"),
    (42, "'42'"),
])
def test_ps_quote_doubles_every_single_quote_powershell_recognizes(raw, literal):
    assert provision.ps_quote(raw) == literal


def test_a_typographic_apostrophe_in_a_list_address_stays_inside_its_string(exchange):
    address = f"o{CURLY}brien-team@example.com"
    fake = exchange(FakeExchange({address: "JOINED"}))

    issues = provision.join_distribution_lists(UPN, [(address, "Team")])

    assert issues == []
    # Every curly quote in the script is doubled: none stands alone to end a literal.
    assert re.search(f"(?<!{CURLY}){CURLY}(?!{CURLY})", fake.script) is None
    assert f"-Identity 'o{CURLY}{CURLY}brien-team@example.com'" in fake.script


def test_every_exchange_command_the_tool_prints_uses_the_shared_quoting(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(provision, "LOG_DIR", tmp_path / "logs")
    curly_upn = f"d{CURLY}angelo@example.com"

    provision.print_dl_joins(curly_upn, [(f"o{CURLY}brien@example.com", "Team")])

    out = capsys.readouterr().out
    assert f"-Identity 'o{CURLY}{CURLY}brien@example.com' -Member 'd{CURLY}{CURLY}angelo@example.com'" in out


def test_convert_shared_quotes_the_upn_the_same_way(exchange):
    scripts = []
    exchange(lambda body: scripts.append(body) or SimpleNamespace(returncode=0))

    provision.convert_mailbox_shared(f"d{CURLY}angelo@example.com")

    assert f"Set-Mailbox -Identity 'd{CURLY}{CURLY}angelo@example.com' -Type Shared" in scripts[0]
