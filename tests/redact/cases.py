"""Known FAKE credentials for AC-09, and texts that must NOT change.

Every fake secret is built by string concatenation, so secret scanners do not flag this repo.
None of them is real. Each case: (name, text, parts that must be gone after redact, a kind
that must be reported or None).
"""

from __future__ import annotations

_B64 = "Zm9vYmFyYmF6cXV4"  # base64-looking filler
_PEM_BODY = "MIIEow" + "IBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gunVTLw7onLRnrq0"
_JWT = (
    "ey"
    + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    + "."
    + "ey"
    + "JzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkZha2UifQ"
    + "."
    + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
_GH = "gh" + "p_" + "A1b2C3d4" * 4 + "E5f6"
_GHS = "gh" + "s_" + "Z9y8X7w6" * 4 + "V5u4"
_GH_PAT = "github" + "_pat_" + "11ABCDEFG0" + "a1B2c3D4e5" * 6
_GLPAT = "gl" + "pat-" + "abcDEF123456ghiJKL78"
_SLACK = "xo" + "xb-" + "1234567890-" + "abcdefghijKLMN"
_STRIPE = "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
_GOOGLE = "AI" + "za" + "SyD" + "Fake1234567890abcdefghijKLMNOPQRS"[:32]
_OPENAI = "sk" + "-proj-" + "Ab1Cd2Ef3" * 4
_OPENAI_OLD = "sk" + "-" + "T3BlbkFJ" + "a1b2c3d4e5" * 4
_ANTHROPIC = "sk" + "-ant-api03-" + "Zx9Wv8Ut7" * 4
_NPM = "np" + "m_" + "Ab1Cd2Ef3Gh4" * 3
_AWS_ID = "AK" + "IA" + "Z3FAKE" + "QWERTYUIOP"
_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYFAKEKEYabcd"[:18]
_RANDOM = "9fX2kLp0Qw7ZrT4m" + "N8vB1cY6hJ3sD5gE"
_BASIC = "dXNlcjpw" + "YXNzd29yZA=="
_DOCKER_AUTH = "ZGVwbG95" + "OnMzY3IzdA=="


def _pw(word: str) -> str:
    return word + "-" + "Pa55" + "w0rd"


SECRET_CASES: list[tuple[str, str, list[str], str | None]] = [
    (
        "pem-rsa",
        "before\n-----BEGIN "
        + "RSA PRIVATE KEY-----\n"
        + _PEM_BODY
        + "\n"
        + _PEM_BODY[::-1]
        + "\n-----END RSA PRIVATE KEY-----\nafter",
        [_PEM_BODY, _PEM_BODY[::-1], "BEGIN RSA"],
        "private-key",
    ),
    (
        "pem-unterminated",
        "key:\n-----BEGIN " + "OPENSSH PRIVATE KEY-----\n" + _PEM_BODY + "\n" + _B64 * 3,
        [_PEM_BODY, _B64],
        "private-key",
    ),
    (
        "pem-escaped-in-json",
        '{"pk": "-----BEGIN ' + "PRIVATE KEY-----\\n" + _PEM_BODY + '\\n-----END PRIVATE KEY-----\\n"}',
        [_PEM_BODY],
        "private-key",
    ),
    ("aws-key-id", "export AWS_ACCESS_KEY_ID=" + _AWS_ID, [_AWS_ID], "aws-access-key-id"),
    ("aws-key-id-in-prose", "the key " + _AWS_ID + " was leaked", [_AWS_ID], "aws-access-key-id"),
    ("aws-secret", "aws_secret_access_key = " + _AWS_SECRET, [_AWS_SECRET], "aws-secret-key"),
    ("aws-secret-json", '{"SecretAccessKey": "' + _AWS_SECRET + '"}', [_AWS_SECRET], None),
    ("github-ghp", "token " + _GH + " in a log", [_GH], "github-token"),
    ("github-ghs", "GITHUB_TOKEN=" + _GHS, [_GHS], "github-token"),
    ("github-pat", "use " + _GH_PAT, [_GH_PAT], "github-token"),
    ("gitlab", "CI uses " + _GLPAT + " to clone", [_GLPAT], "gitlab-token"),
    ("slack", "SLACK=" + _SLACK, [_SLACK], "slack-token"),
    ("stripe", "stripe key " + _STRIPE, [_STRIPE], "stripe-key"),
    ("google", "maps: " + _GOOGLE, [_GOOGLE], "google-api-key"),
    ("openai-proj", "OPENAI_API_KEY=" + _OPENAI, [_OPENAI], None),
    ("openai-old", "key " + _OPENAI_OLD + " here", [_OPENAI_OLD], "openai-key"),
    ("anthropic", "ANTHROPIC_API_KEY=" + _ANTHROPIC, [_ANTHROPIC], None),
    ("anthropic-prose", "my key is " + _ANTHROPIC + ".", [_ANTHROPIC], "anthropic-key"),
    ("jwt", "id_token " + _JWT, [_JWT, _JWT.split(".")[2]], "jwt"),
    (
        "bearer-header",
        "Authorization: Bearer " + "opaque" + "Tok3n123456789xyz",
        ["opaqueTok3n123456789xyz"],
        None,
    ),
    ("basic-header", "Authorization: Basic " + _BASIC, [_BASIC], "authorization"),
    (
        "curl-header",
        'curl -H "Authorization: Bearer ' + _JWT + '" https://api.example.com/v1',
        [_JWT],
        None,
    ),
    ("bearer-alone", "sent Bearer " + "abc123DEF456ghi789" + " to the API", ["abc123DEF456ghi789"], "bearer"),
    (
        "url-password",
        "git clone https://deploy:" + "S3cr3tPa55" + "@git.example.com/org/repo.git",
        ["S3cr3tPa55"],
        "url-password",
    ),
    ("postgres", "DATABASE_URL=postgres://app:" + _pw("pg") + "@db:5432/app", [_pw("pg")], None),
    (
        "postgres-prose",
        "connect to postgresql://app:" + _pw("pg2") + "@db/app",
        [_pw("pg2")],
        "connection-string",
    ),
    ("mysql", "mysql://root:" + _pw("my") + "@db/app", [_pw("my")], "connection-string"),
    ("mongodb-srv", "uri mongodb+srv://admin:" + _pw("mongo") + "@c0.example.net/db", [_pw("mongo")], None),
    ("redis", "redis://:" + _pw("redis") + "@cache:6379/0", [_pw("redis")], "connection-string"),
    ("amqp", "amqp://guest:" + _pw("rabbit") + "@mq:5672/", [_pw("rabbit")], "connection-string"),
    (
        "ado-connection",
        "Server=db;User Id=sa;Password=" + _pw("sql") + ";Encrypt=true",
        [_pw("sql")],
        "password",
    ),
    (
        "jdbc-query",
        "jdbc:postgresql://db/app?user=app&password=" + _pw("jdbc") + "&ssl=true",
        [_pw("jdbc")],
        None,
    ),
    ("env-password", "DB_PASSWORD=" + _pw("env"), [_pw("env")], "password"),
    ("env-export", "export API_TOKEN=" + _pw("tok"), [_pw("tok")], "token"),
    ("yaml-password", "db:\n  password: " + _pw("yaml") + "\n  user: app", [_pw("yaml")], "password"),
    ("yaml-quoted", "password: '" + _pw("q") + "'", [_pw("q")], "password"),
    ("json-secret", '{"client_secret": "' + _pw("cs") + '", "id": 1}', [_pw("cs")], "secret"),
    ("json-spaced-key", '{"DB Password": "' + _pw("sp") + '"}', [_pw("sp")], "password"),
    ("api-key", "api_key=" + _pw("ak"), [_pw("ak")], "api-key"),
    ("access-key-yaml", "access_key: " + _pw("acc"), [_pw("acc")], "api-key"),
    ("private-key-py", "private_key = '" + _pw("pk") + "'", [_pw("pk")], "private-key"),
    ("camel-case", "{apiKey: " + _pw("camel") + ", user: x}", [_pw("camel")], "api-key"),
    ("header-x-api-key", "X-Api-Key: " + _pw("hdr"), [_pw("hdr")], "api-key"),
    ("pgpassword", "PGPASSWORD=" + _pw("pg3") + " psql -h db", [_pw("pg3")], "password"),
    ("logfmt", 'level=info msg="login ok" token=' + _pw("lf") + " user=bob", [_pw("lf")], "token"),
    ("cli-flag", "mysql --password " + _pw("flag") + " -h db", [_pw("flag")], "password"),
    ("cli-flag-quoted", 'tool --api-token "' + _pw("fq") + '" run', [_pw("fq")], "token"),
    (
        "cli-flag-equals",
        "kubectl create secret generic x --from-literal=password=" + _pw("lit"),
        [_pw("lit")],
        None,
    ),
    (
        "kubeconfig",
        "users:\n- name: admin\n  user:\n    client-certificate-data: "
        + _B64 * 4
        + "\n    client-key-data: "
        + _B64[::-1] * 4
        + "\n    token: "
        + _pw("kube")
        + "\n",
        [_B64, _B64[::-1], _pw("kube")],
        "kubeconfig-key",
    ),
    (
        "k8s-secret-yaml",
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ntype: Opaque\ndata:\n  username: YWRtaW4=\n"
        "  pass: " + _B64 + "\nstringData:\n  url: " + _pw("sd") + "\n  config.yaml: |\n    user: app\n"
        "    pass: " + _pw("block") + "\n",
        [_B64, _pw("sd"), _pw("block"), "YWRtaW4="],
        "k8s-secret",
    ),
    (
        "k8s-secret-json",
        '{"apiVersion": "v1", "kind": "Secret", "data": {"user": "YWRtaW4=", "pass": "' + _B64 + '"}}',
        [_B64, "YWRtaW4="],
        "k8s-secret",
    ),
    (
        "k8s-env-pair",
        "env:\n  - name: DB_PASSWORD\n    value: "
        + _pw("envpair")
        + "\n  - name: LOG_LEVEL\n    value: info",
        [_pw("envpair")],
        "password",
    ),
    (
        "docker-config",
        '{"auths": {"registry.example.com": {"auth": "' + _DOCKER_AUTH + '"}}}',
        [_DOCKER_AUTH],
        "auth",
    ),
    ("npmrc", "//registry.npmjs.org/:_authToken=" + _NPM, [_NPM], "npm-token"),
    ("npmrc-plain", "//npm.example.com/:_authToken=" + _pw("npm"), [_pw("npm")], "token"),
    ("random-after-word", "the deploy token is " + _RANDOM, [_RANDOM], "high-entropy"),
    ("random-after-secret", "secret: " + _RANDOM + " (rotate it)", [_RANDOM], None),
    # Slashes alone do not make a path: base64 (capitals) and word-less segments stay secrets.
    (
        "base64-with-slashes",
        "key: " + "dGhpcy9pcy9hL2Jhc2U2" + "NC9zZWNyZXQvdmFsdWU=",
        ["dGhpcy9pcy9hL2Jhc2U2"],
        None,
    ),
    (
        "slashes-no-words",
        "key: " + "q7w8e9r0/t1y2u3i4o5p6/" + "a7s8d9f0g1h2j3k4",
        ["q7w8e9r0/t1y2u3i4o5p6/"],
        None,
    ),
    (
        "yaml-block-password",
        "tls:\n  private_key: |\n    " + _B64 + "\n    " + _B64[::-1] + "\n  cert: public\n",
        [_B64, _B64[::-1]],
        "private-key",
    ),
    ("git-credentials", "https://bob:" + _GH + "@github.com", [_GH], "github-token"),
    ("password-in-sentence", "set password=" + _pw("s") + " then restart", [_pw("s")], "password"),
    ("comment-password", "# password: " + _pw("c"), [_pw("c")], "password"),
]

# Texts that must come back unchanged, with a count of 0.
FALSE_POSITIVES: list[str] = [
    # A Grimoire key after the word "Key" (seen in a real prompt): a path of words, not a secret.
    "Key: infra/k8s/flux/flux-reference-bootstrap-sources-image-automation-manifests.md · found by: search",
    "secret file: deploy/charts/my-service/templates/sealed-secret-for-production.yaml",
    "commit 3f786850e387550fdab836ed7e6dc881de23001b Merge branch 'main'",
    "image: nginx@sha256:" + "4c0f" * 16,
    "imageID: docker.io/library/redis@sha256:" + "a1b2" * 16,
    "uid: 123e4567-e89b-12d3-a456-426614174000",
    "We use a token bucket for rate limits.",
    "Open the password reset page and click Send.",
    "passwordless login is enabled",
    "author: Jane Doe <jane@example.com>",
    "Author: Jane Doe",
    "tokenizer: bpe",
    "max_tokens: 4096",
    "token_expiry: 3600",
    "secretName: tls-cert",
    "valueFrom:\n  secretKeyRef:\n    name: db-creds\n    key: password\n",
    "imagePullSecrets:\n  - name: regcred\n",
    "DB_PASSWORD: ${DB_PASSWORD}",
    "password: {{ .Values.db.password }}",
    "password: <your password>",
    "password:",
    'password: ""',
    "auth: true",
    "PWD=/home/dm/project",
    "https://github.com/org/repo.git and git@github.com:org/repo.git",
    "http://localhost:8080/health",
    "https://user@example.com/path",
    "Error: invalid token: expected 3 parts",
    "      DB_PASSWORD:  <set to the key 'password' in secret 'db-creds'>  Optional: false",
    "Liveness probe failed: HTTP probe failed with statuscode: 500",
    "ghp_ is the prefix of GitHub tokens",
    "The AKIA prefix marks AWS access keys.",
    "Bearer authentication is used by the API.",
    "Authorization: Bearer",
    "Use the secret my-very-long-secret-name-for-the-database-v1 in prod",
    "if password == expected:",
    "password = get_password()",
    "the API key rotation runbook",
    "kind: Secret\nmetadata:\n  name: db\n",
    "disk-usage-dashboard-for-production-cluster-01",
    "Back-off restarting failed container api in pod api-7d9f-xk2p",
    "[REDACTED:password] and [REDACTED:jwt]",
]
