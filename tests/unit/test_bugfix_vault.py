"""Regression tests for verified vault bugs: BUG-007, BUG-008, BUG-031."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kmcp.utils import vault


@pytest.fixture(autouse=True)
def _isolated_vault(tmp_path, monkeypatch):
    """Per-test vault root so no test touches the repo's real vault."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.ensure_vault_dirs()
    yield


# ── BUG-007: delete must remove the JSON mirror ──────────────────────────────


def test_delete_removes_mirror_with_relative_vault_root(tmp_path, monkeypatch):
    # VAULT_ROOT is relative by default; that is what broke the old
    # containment check against mirror.resolve().parents.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(vault, "VAULT_ROOT", Path("vault"))
    vault.ensure_vault_dirs()

    vault.vault_write("probe", "row-one", "TOPSECRET")
    mirror = Path(vault.vault_path("probe", "row-one"))
    assert mirror.is_file()

    result = vault.vault_delete("probe", "row-one")
    assert result["deleted"] is True
    assert not mirror.exists()

    assert json.loads(vault.vault_read("probe", "row-one")) == {"error": "not found"}


# ── BUG-008: realistic secrets must be fully redacted ────────────────────────


def test_redact_password_with_mixed_punctuation():
    out = vault.redact_secrets("password: S3cr3t+P@ss!")
    assert out == "password: [REDACTED]"
    assert "S3cr3t" not in out


def test_redact_quoted_password_with_mixed_punctuation():
    out = vault.redact_secrets('password="S3cr3t+P@ss!"')
    assert out == 'password="[REDACTED]"'
    assert "S3cr3t" not in out


def test_redact_jwt_token_full_value():
    token = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out = vault.redact_secrets(f"token={token}")
    assert out == "token=[REDACTED]"
    assert "eyJhbGci" not in out
    assert "eyJzdWI" not in out
    assert "SflKxw" not in out


def test_redact_aws_secret_access_key():
    value = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY="
    out = vault.redact_secrets(f"AWS_SECRET_ACCESS_KEY={value}")
    assert out == "AWS_SECRET_ACCESS_KEY=[REDACTED]"
    assert "wJalrXUtnFEMI" not in out


def test_redaction_preserves_legitimate_content():
    text = "password policy: keep this note; token usage is documented."
    assert vault.redact_secrets(text) == text


def test_redact_prefixed_secret_keys():
    out = vault.redact_secrets("db_password=S3cr3t+P@ss! api_token=abc.def/ghi==")
    assert out == "db_password=[REDACTED] api_token=[REDACTED]"
    assert "S3cr3t" not in out and "abc.def" not in out


# ── BUG-031: secret refs must resolve to their stored value ──────────────────


def test_secret_ref_round_trip_resolves_encrypted_value(monkeypatch):
    monkeypatch.setenv("MCP_VAULT_PASSPHRASE", "unit-test-passphrase")
    secret = "S3cr3t+P@ss!"

    ref = vault.write_secret_ref("svc/user", secret)
    assert ref == "secret:svc/user"

    stored = json.loads(vault.vault_read("secrets", "svc/user"))
    assert stored["ref"] == ref
    assert stored["redacted"] is True
    assert secret not in json.dumps(stored)  # encrypted at rest
    assert vault.resolve_secret_ref(ref) == secret


def test_secret_ref_without_value_still_resolves_none():
    vault.vault_write(
        "secrets",
        "legacy-no-value",
        {"ref": "secret:legacy-no-value", "redacted": True},
        tags="secret",
    )
    assert vault.resolve_secret_ref("secret:legacy-no-value") is None


def test_secret_ref_legacy_plaintext_value_still_resolves():
    vault.vault_write(
        "secrets",
        "legacy-plain",
        {"ref": "secret:legacy-plain", "value": "plain-legacy-value"},
        tags="secret",
    )
    assert vault.resolve_secret_ref("secret:legacy-plain") == "plain-legacy-value"
