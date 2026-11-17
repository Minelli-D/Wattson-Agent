"""Secret redaction (SEC-04, SEC-05, AC-09) and secret-like file names."""

from __future__ import annotations

import time

import pytest

from wattson.shared.redact import Redaction, is_secret_path, marker, redact

from .cases import FALSE_POSITIVES, SECRET_CASES


@pytest.mark.parametrize(("name", "text", "parts", "kind"), SECRET_CASES, ids=[c[0] for c in SECRET_CASES])
def test_known_fake_secret_is_redacted(name: str, text: str, parts: list[str], kind: str | None) -> None:
    result = redact(text)
    for part in parts:
        assert part not in result.text, f"{name}: a secret part is still there"
    assert result.count >= 1
    assert "[REDACTED:" in result.text
    if kind:
        assert kind in result.kinds


@pytest.mark.parametrize("text", FALSE_POSITIVES)
def test_ordinary_text_is_not_changed(text: str) -> None:
    result = redact(text)
    assert result.text == text
    assert result.count == 0
    assert result.kinds == []


@pytest.mark.parametrize(("name", "text", "parts", "kind"), SECRET_CASES, ids=[c[0] for c in SECRET_CASES])
def test_redaction_is_idempotent(name: str, text: str, parts: list[str], kind: str | None) -> None:
    once = redact(text)
    twice = redact(once.text)
    assert twice.text == once.text
    assert twice.count == 0


def test_all_cases_together_leave_nothing() -> None:
    text = "\n\n".join(c[1] for c in SECRET_CASES)
    result = redact(text)
    for _, _, parts, _ in SECRET_CASES:
        for part in parts:
            assert part not in result.text
    assert result.count >= len(SECRET_CASES)


def test_result_shape() -> None:
    result = redact("password=" + "abc" + "123 and token=" + "xyz" + "789")
    assert isinstance(result, Redaction)
    assert result.count == 2
    assert result.kinds == ["password", "token"]  # sorted, each once
    assert redact("") == Redaction("", 0, [])
    assert marker("jwt") == "[REDACTED:jwt]"


def test_context_around_the_secret_is_kept() -> None:
    hidden = "Bearer " + "abc123" + "DEF456ghi789xyz"
    assert redact("Authorization: " + hidden).text == "Authorization: Bearer [REDACTED:authorization]"
    url = "postgres://app:" + "pw" + "123@db:5432/app"
    assert redact(url).text == "postgres://app:[REDACTED:connection-string]@db:5432/app"
    assert redact("x\nDB_PASSWORD=" + "s3" + "cret\ny").text == "x\nDB_PASSWORD=[REDACTED:password]\ny"


def test_pem_block_is_removed_whole_and_unterminated_runs_to_the_end() -> None:
    begin = "-----BEGIN " + "EC PRIVATE KEY-----"
    end = "-----END EC PRIVATE KEY-----"
    result = redact("a\n" + begin + "\nAAAA\nBBBB\n" + end + "\nb")
    assert result.text == "a\n[REDACTED:private-key]\nb"
    result = redact("a\n" + begin + "\nAAAA\nBBBB\nstill key")
    assert result.text == "a\n[REDACTED:private-key]"


def test_public_certificate_is_kept() -> None:
    cert = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
    assert redact(cert).count == 0


def _mixed_megabyte() -> str:
    lines = [
        "2026-10-22T10:00:00Z INFO request done path=/api/v1/items status=200 duration=12ms user=bob",
        "2026-10-22T10:00:01Z ERROR invalid token: expected 3 parts",
        "  Warning  BackOff  3m  kubelet  Back-off restarting failed container api in pod api-7d9f-xk2p",
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  replicas: 2",
        "    env:\n    - name: LOG_LEVEL\n      value: info",
        "The password reset page uses a token bucket; see https://example.com/docs.",
        "commit 3f786850e387550fdab836ed7e6dc881de23001b",
        *(c[1] for c in SECRET_CASES[::6]),
    ]
    chunk = "\n".join(lines) + "\n"
    return (chunk * (1_000_000 // len(chunk) + 1))[:1_000_000]


def _best_of_three(text: str) -> float:
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        redact(text)
        best = min(best, time.perf_counter() - start)
    return best


def test_one_megabyte_in_under_300_ms() -> None:
    text = _mixed_megabyte()
    assert len(text) == 1_000_000
    assert _best_of_three(text) < 0.3


@pytest.mark.parametrize(
    "text",
    [
        "a" * 1_000_000,
        "eyJ" * 333_334,
        "eyJabcdefgh." * 83_334,
        "github_pat_" * 90_910,
        "glpat-" * 166_667,
        "AIza" * 250_000,
        "key: " * 200_000,
        "password=" * 111_112,
        "-" * 1_000_000,
        "token " * 166_667,
        "http://a:b" * 100_000,
        "sk_live_" * 125_000,
    ],
    ids=lambda t: repr(t[:12]),
)
def test_no_catastrophic_backtracking(text: str) -> None:
    assert _best_of_three(text) < 0.3


@pytest.mark.parametrize(
    "text",
    [
        ("kind: Secret\ndata:\n  a: b\n" * 40_000)[:1_000_000],
        '{"token": "abc", "name": "x"}, ' * 33_000,
        "--password x " * 77_000,
    ],
    ids=["secret-manifests", "json-tokens", "flags"],
)
def test_tens_of_thousands_of_secrets_stay_linear(text: str) -> None:
    # Real work per secret (each one is replaced), so a looser bound; quadratic time would be minutes.
    result = redact(text)
    assert result.count > 30_000
    assert _best_of_three(text) < 1.0


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "app/.env",
        ".env.local",
        ".env.production",
        "server.pem",
        "certs/tls.key",
        "store.p12",
        "cert.pfx",
        "id_rsa",
        "id_rsa.pub",
        "/home/u/.ssh/id_ed25519",
        "id_ecdsa",
        "vault.kdbx",
        "kubeconfig",
        "kubeconfig-prod.yaml",
        "/home/u/.kube/config",
        "credentials",
        "credentials.json",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "/home/u/.docker/config.json",
        "terraform.tfstate",
        "infra/terraform.tfstate.backup",
        "prod.tfvars",
        "secret.yaml",
        "secrets.yml",
        "secrets.json",
        "secrets.prod.yaml",
        "k8s/db-secret.yaml",
        "C:\\Users\\u\\.kube\\config",
        "APP/.ENV",
    ],
)
def test_secret_paths(path: str) -> None:
    assert is_secret_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "app/.env.sample",
        "README.md",
        "src/main.py",
        "settings.env",
        "src/secretary.py",
        "docs/secrets.md",
        "keyboard.py",
        "app/config.json",
        "k8s/deployment.yaml",
        "Dockerfile",
        "src/wattson/shared/redact.py",
        "tokens.py",
        "",
    ],
)
def test_ordinary_paths(path: str) -> None:
    assert not is_secret_path(path)
