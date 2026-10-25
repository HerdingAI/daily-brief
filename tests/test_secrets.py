import pytest

from daily_brief import secrets


def test_load_secrets_ignores_comments_and_missing_file(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("# comment\n\nA=1\nB = \"two\" \nexport C=3\nbad line\n")
    assert secrets.load(p) == {"A": "1", "B": "two", "C": "3"}
    assert secrets.load(tmp_path / "nope.env") == {}


def test_require_raises_without_leaking_values(tmp_path):
    p = tmp_path / "s.env"
    p.write_text("A=topsecret\n")
    assert secrets.require(p, "A") == "topsecret"
    with pytest.raises(secrets.SecretMissing) as e:
        secrets.require(p, "B")
    assert "topsecret" not in str(e.value) and "B" in str(e.value)
