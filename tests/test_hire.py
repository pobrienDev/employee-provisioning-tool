"""Tests for reading hire.yaml (provision.load_hire).

The file is written to a temp directory standing in for the repo root, so
these never touch a real hire.yaml.

Run from the repo root:  python -m pytest -q
"""

import pytest

import provision
from provision import ProvisionError, load_hire


@pytest.fixture
def hire_file(monkeypatch, tmp_path):
    monkeypatch.setattr(provision, "BASE_DIR", tmp_path)

    def write(text):
        (tmp_path / "hire.yaml").write_text(text, encoding="utf-8")

    return write


def test_values_are_read_and_stripped(hire_file):
    hire_file("first_name: ' Taylor '\nlast_name: Example\ntitle: ' Property Manager'\n")
    hire = load_hire()
    assert (hire["first_name"], hire["last_name"], hire["title"]) == ("Taylor", "Example", "Property Manager")


# --- YAML 1.1 surprises ---------------------------------------------------------------

@pytest.mark.parametrize("line, field", [
    ("last_name: No", "last_name"),       # a romanized Korean surname
    ("first_name: Yes", "first_name"),
    ("title: Off", "title"),
])
def test_a_name_yaml_reads_as_a_boolean_is_refused_with_a_hint(hire_file, line, field):
    hire_file(f"first_name: Taylor\nlast_name: Example\n{line}\n")
    with pytest.raises(ProvisionError, match=f"{field} reads as a yes/no value"):
        load_hire()


def test_a_quoted_no_is_a_perfectly_good_surname(hire_file):
    hire_file("first_name: Yu\nlast_name: \"No\"\n")
    assert load_hire()["last_name"] == "No"


@pytest.mark.parametrize("raw, expected", [
    ("050", "050"),      # YAML 1.1 octal would have made this 40
    ("619", "619"),
    ("720/721", "720/721"),
    ("1e3", "1e3"),
])
def test_property_numbers_stay_text(hire_file, raw, expected):
    hire_file(f"first_name: T\nlast_name: E\nproperty_number: {raw}\n")
    assert load_hire()["property_number"] == expected


def test_booleans_still_work_where_they_belong(hire_file):
    hire_file("first_name: T\nlast_name: E\ncopy_rpm: yes\nplatforms:\n  yardi: yes\n  happyco: no\n")
    hire = load_hire()
    assert hire["copy_rpm"] is True
    assert hire["platforms"] == {"yardi": True, "happyco": False}


# --- required fields ------------------------------------------------------------------

def test_a_whitespace_only_first_name_counts_as_missing(hire_file):
    hire_file("first_name: ' '\nlast_name: Smith\n")
    with pytest.raises(ProvisionError, match="missing first_name"):
        load_hire()


def test_both_names_missing_are_named_together(hire_file):
    hire_file("title: Manager\n")
    with pytest.raises(ProvisionError, match="missing first_name, last_name"):
        load_hire()


def test_an_absent_file_says_how_to_make_one(hire_file, tmp_path):
    with pytest.raises(ProvisionError, match="hire.yaml not found"):
        load_hire()


def test_a_non_mapping_file_is_rejected(hire_file):
    hire_file("- just\n- a list\n")
    with pytest.raises(ProvisionError, match="must be a mapping"):
        load_hire()
