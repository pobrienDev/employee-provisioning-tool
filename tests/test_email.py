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


# --- the committed example template ------------------------------------------------

def test_example_template_renders_with_a_clean_subject(draft_env, monkeypatch, capsys):
    drafts = []
    monkeypatch.setattr(
        provision, "create_outlook_draft",
        lambda to, cc, subject, body, attachments: drafts.append((to, cc, subject, body)) or (None, []),
    )

    provision.email_draft(HIRE, "Taylor Example", "texample@example.com", "Pw-1!", config={}, open_draft=True)

    to, cc, subject, body = drafts[0]
    # The example's first line reads "Subject: Login details for {name}";
    # the label is for the reader, not for the subject field.
    assert subject == "Login details for Taylor Example"
    assert not subject.lower().startswith("subject")
    assert to == "manager619@example.com" and cc is None
    assert "Username:           texample@example.com" in body
    assert "Temporary password: Pw-1!" in body
    assert body.startswith("Hello Taylor Example")
    assert "Login details for Taylor Example" in capsys.readouterr().out


def test_a_template_without_the_label_keeps_its_first_line_as_subject(draft_env, monkeypatch, tmp_path):
    (tmp_path / "email_template.txt").write_text("Welcome {first}\n\nUser: {username}\n", encoding="utf-8")
    drafts = []
    monkeypatch.setattr(
        provision, "create_outlook_draft",
        lambda to, cc, subject, body, attachments: drafts.append(subject) or (None, []),
    )

    provision.email_draft(HIRE, "Taylor Example", "t@example.com", "Pw", config={}, open_draft=True)

    assert drafts == ["Welcome Taylor"]


def test_rpm_is_copied_only_when_the_form_asks(draft_env, monkeypatch):
    drafts = []
    monkeypatch.setattr(
        provision, "create_outlook_draft",
        lambda to, cc, subject, body, attachments: drafts.append(cc) or (None, []),
    )
    hire = dict(HIRE, rpm_email="rpm@example.com", copy_rpm=True)

    provision.email_draft(hire, "Taylor Example", "t@example.com", "Pw", config={}, open_draft=True)
    provision.email_draft(dict(hire, copy_rpm=False), "Taylor Example", "t@example.com", "Pw", config={}, open_draft=True)

    assert drafts == ["rpm@example.com", None]


def test_no_login_info_address_means_no_draft(draft_env, capsys):
    provision.email_draft({"first_name": "T", "last_name": "E"}, "T E", "te@example.com", "Pw", config={})

    assert "login-info email draft" not in capsys.readouterr().out
    assert draft_env == []


def test_a_bad_placeholder_is_a_clear_error(draft_env, tmp_path):
    (tmp_path / "email_template.txt").write_text("Subject: hi\n\n{nonsense}\n", encoding="utf-8")

    with pytest.raises(provision.ProvisionError, match="placeholder problem"):
        provision.email_draft(HIRE, "Taylor Example", "t@example.com", "Pw", config={})
