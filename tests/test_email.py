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


# --- the Outlook draft: built in the operator's mailbox ------------------------------

class FakeDelegated:
    """Stand-in for DelegatedGraphClient: records the draft and attachments;
    `fail` names a file whose upload raises after the draft exists."""

    def __init__(self, fail=None):
        self.fail = fail
        self.drafts = []
        self.attached = []

    def create_draft(self, payload):
        self.drafts.append(payload)
        return {"id": "msg-1", "webLink": "https://outlook.example/msg-1"}

    def add_file_attachment(self, message_id, name, data, **kwargs):
        if name == self.fail:
            raise provision.GraphError("Graph API error (413) — too large", status=413)
        self.attached.append((message_id, name, len(data), kwargs))


@pytest.fixture
def delegated(monkeypatch):
    fake = FakeDelegated()
    monkeypatch.setattr(provision.DelegatedGraphClient, "from_env", classmethod(lambda cls: fake))
    monkeypatch.setattr(provision, "load_signature", lambda: (None, []))
    return fake


def test_draft_carries_recipients_body_and_attachments(delegated, tmp_path):
    pdf = tmp_path / "MFA Instructions.pdf"
    pdf.write_bytes(b"%PDF" * 10)

    link, warnings = provision.create_outlook_draft(
        "to@example.com", "rpm@example.com; boss@example.com", "Hi", "Body https://outlook.office.com", [str(pdf)],
    )

    assert link == "https://outlook.example/msg-1" and warnings == []
    draft = delegated.drafts[0]
    assert draft["subject"] == "Hi"
    assert draft["toRecipients"] == [{"emailAddress": {"address": "to@example.com"}}]
    assert [r["emailAddress"]["address"] for r in draft["ccRecipients"]] == ["rpm@example.com", "boss@example.com"]
    assert '<a href="https://outlook.office.com">' in draft["body"]["content"]
    assert delegated.attached == [("msg-1", "MFA Instructions.pdf", 40, {})]


def test_missing_and_oversized_files_are_warned_about_before_the_draft_exists(delegated, monkeypatch, tmp_path):
    big = tmp_path / "video.mp4"
    big.write_bytes(b"0" * 200)
    monkeypatch.setattr(provision, "ATTACHMENT_UPLOAD_LIMIT", 100)

    _, warnings = provision.create_outlook_draft("to@example.com", None, "Hi", "Body", [str(tmp_path / "nope.pdf"), str(big)])

    assert len(delegated.drafts) == 1 and delegated.attached == []
    assert warnings[0].startswith("attachment not found")
    assert "over Outlook's 150 MB limit" in warnings[1]


def test_a_failed_upload_after_the_draft_exists_is_reported_as_incomplete(delegated, draft_env, capsys, tmp_path):
    ok = tmp_path / "a.pdf"
    ok.write_bytes(b"a")
    bad = tmp_path / "b.pdf"
    bad.write_bytes(b"b")
    delegated.fail = "b.pdf"

    provision.email_draft(
        HIRE, "Taylor Example", "t@example.com", "Pw", config={"email_attachments": [str(ok), str(bad)]}, open_draft=True,
    )

    out = capsys.readouterr().out
    assert [a[1] for a in delegated.attached] == ["a.pdf"]
    assert "outlook draft: could not attach b.pdf" in out
    assert "draft created but incomplete" in out
    assert "https://outlook.example/msg-1" in out
