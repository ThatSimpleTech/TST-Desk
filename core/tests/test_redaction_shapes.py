"""Redaction knows the common secret shapes, and the ledger is redacted (SECUX-01, SECUX-02).

The patterns covered sk- keys, GitHub tokens, AWS key ids and a private
key's BEGIN line. Stripe, Slack, Google, GitLab and Hugging Face keys,
JWTs, Bearer tokens, env-style passwords and connection strings passed
through every "redacted" output, and a PEM block lost only its header. The
decisions ledger, written into the workspace, was not redacted at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.autonomy.ledger import DecisionLedger, LedgerEntry, format_entry
from tstd.logging import REDACTED, redact_secrets

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF0qHqsZ\n"
    "kIx0h0Ph0w==\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("sk_live_" + "a1B2c3D4e5F6g7H8i9", ""),
        ("rk_test_" + "a1B2c3D4e5F6g7H8i9", ""),
        ("xoxb-" + "123456789012-abcdefghijkl", ""),
        ("AIza" + "SyA1234567890abcdefghijklmnopqrstuv", ""),
        ("glpat-" + "abcdefghijklmnopqrstu", ""),
        ("hf_" + "abcdefghijklmnopqrstuvwxyz123456", ""),
        (JWT, ""),
        ("Authorization: Bearer " + "abcdef0123456789xyz", "Authorization: Bearer "),
        ("DB_PASSWORD=" + "hunter2", "DB_PASSWORD="),
        ('API_TOKEN: "' + "abc123def456" + '"', 'API_TOKEN: "'),
        ("postgres://admin:" + "S3cretPass" + "@db:5432/app", "postgres://admin:"),
        ("mongodb+srv://u:" + "pw123456" + "@cluster0.example.net", "mongodb+srv://u:"),
    ],
)
def test_each_shape_is_redacted(text: str, kept: str) -> None:
    out = redact_secrets(text)
    assert REDACTED in out
    assert out.startswith(kept)
    secret = text[len(kept) :].strip('"').split("@")[0]
    assert secret not in out


def test_a_whole_pem_block_goes(tmp_path: Path) -> None:
    out = redact_secrets(f"key:\n{PEM}\nafter")
    assert "MIIEow" not in out and "END RSA PRIVATE KEY" not in out
    assert out.endswith("after")


@pytest.mark.parametrize(
    "text",
    [
        "token = get_token()",
        "API_TOKEN=$SECRET",
        "API_TOKEN=${SECRET}",
        "password policy: strong",
        "user_password_hash = h(x)",
        "the bearer of bad news",
        "https://example.com/path",
    ],
)
def test_ordinary_code_and_prose_are_left_alone(text: str) -> None:
    assert redact_secrets(text) == text


async def test_the_ledger_in_the_workspace_is_redacted(tmp_path: Path) -> None:
    ledger = DecisionLedger(tmp_path)
    key = "sk-proj-" + "Zq8Xw2Lm9Pt4Rv7Ns1Kb6Jd3"
    what = f'fs_write {{"content": "API_KEY={key}", "path": "notes.env"}}'
    await ledger.append(LedgerEntry(decision_class="B", what=what, why="asked"))
    text = ledger.path.read_text(encoding="utf-8")
    assert key not in text and REDACTED in text
    assert key not in format_entry(LedgerEntry(decision_class="B", what=what, why=key))
