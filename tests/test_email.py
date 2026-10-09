"""Tests for the login-info email (provision.email_draft and its helpers).

No network and no clipboard: copy_draft_to_clipboard and
create_outlook_draft are replaced per test, and the template is read from a
temp directory standing in for the repo root.

Run from the repo root:  python -m pytest -q
"""

import shutil
from pathlib import Path

import pytest

import provision

REPO = Path(provision.__file__).parent
HIRE = {"first_name": "Taylor", "last_name": "Example", "login_info_email": "manager619@example.com"}


@pytest.fixture
def draft_env(monkeypatch, tmp_path):
    """A repo root holding only the committed example template, a temp log
    dir, and a recorded (never real) clipboard copy."""
    shutil.copy(REPO / "email_template.example.txt", tmp_path / "email_template.example.txt")
    monkeypatch.setattr(provision, "BASE_DIR", tmp_path)
    monkeypatch.setattr(provision, "LOG_DIR", tmp_path / "logs")
    copied = []
    monkeypatch.setattr(provision, "copy_draft_to_clipboard", lambda body: copied.append(body) or True)
    return copied


def test_banner_says_where_the_password_goes(draft_env, capsys):
    provision.email_draft(HIRE, "Taylor Example", "texample@example.com", "Pw-1!", config={})

    out = capsys.readouterr().out
    # The password is on the clipboard, so the banner must not promise
    # "not saved" — it says what carries the credential instead.
    assert "not saved" not in out
    assert "temporary password included" in out and "clipboard" in out
    assert draft_env and "Pw-1!" in draft_env[0]


def test_open_draft_says_the_draft_holds_the_password(draft_env, monkeypatch, capsys):
    monkeypatch.setattr(
        provision, "create_outlook_draft",
        lambda to, cc, subject, body, attachments: ("https://outlook.example/draft", []),
    )

    provision.email_draft(HIRE, "Taylor Example", "texample@example.com", "Pw-1!", config={}, open_draft=True)

    out = capsys.readouterr().out
    assert "holds the temporary password until you send or delete it" in out
    assert "https://outlook.example/draft" in out
