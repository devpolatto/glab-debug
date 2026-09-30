import pytest

from glab_debug.redact import MASK, redact


def one(text: str) -> str:
    out, _ = redact([text])
    return out[0]


def test_env_do_kubernetes_redige_o_value_da_linha_seguinte():
    lines = [
        "+           - name: DB_PASSWORD",
        '+             value: "s3nh4-f0rte"',
        "+           - name: LOG_LEVEL",
        "+             value: info",
    ]
    out, count = redact(lines)
    assert out[1] == f"+             value: {MASK}"
    assert out[3] == "+             value: info"
    assert count == 1


def test_env_so_redige_o_value_imediatamente_seguinte():
    out, count = redact(["- name: API_TOKEN", "  valueFrom:", "    value: nao-e-o-dele"])
    assert out[2] == "    value: nao-e-o-dele"
    assert count == 0


@pytest.mark.parametrize(
    "text, expected",
    [
        ("AWS_SECRET_ACCESS_KEY=abc123", f"AWS_SECRET_ACCESS_KEY={MASK}"),
        ("SECRET_KEY_JWT: 395fd949", f"SECRET_KEY_JWT: {MASK}"),
        ('"password": "hunter2",', f'"password": {MASK},'),
        ("PRIVATE-TOKEN: abcdef", f"PRIVATE-TOKEN: {MASK}"),
        ("export SSO_API_KEY='x.y'", f"export SSO_API_KEY={MASK}"),
        ("CHROMA_PASSWORD=w9us6q ok", f"CHROMA_PASSWORD={MASK} ok"),
    ],
)
def test_atribuicao_com_nome_sensivel(text, expected):
    assert one(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("amqp://user:oBpLpD@rabbitmq:5672", f"amqp://user:{MASK}@rabbitmq:5672"),
        ("redis://:8vKv48@redis-master:6379/0", f"redis://:{MASK}@redis-master:6379/0"),
        ("postgres://admin:46162c@10.20.40.62:5432/db", f"postgres://admin:{MASK}@10.20.40.62:5432/db"),
    ],
)
def test_userinfo_em_url(text, expected):
    assert one(text) == expected


def test_url_sensivel_em_env_e_redigida_por_inteiro():
    out, count = redact(["  - name: DB_URL", "    value: postgres://u:p@h/db"])
    # DB_URL não é nome sensível, mas a senha na URL é.
    assert out[1] == f"    value: postgres://u:{MASK}@h/db"
    assert count == 1


def test_literais_reconheciveis_pelo_valor():
    assert one("key AKIAW3MD7PI2HI4YJRMF aqui") == "key <redacted:aws-key> aqui"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    assert one(f"Bearer? {jwt}") == "Bearer? <redacted:jwt>"
    assert one("token glpat-abcdefghij0123456789xyz") == "token <redacted:gitlab-token>"


def test_header_authorization():
    assert one("Authorization: Bearer abc.def") == f"Authorization: Bearer {MASK}"


@pytest.mark.parametrize(
    "text",
    [
        "        - name: portal-govone-africa-portal",
        '          image: "harbor.govone.digital/cix/portal:244-main-0541f926"',
        "      secretName: portal-govone-africa-tls",
        "        secretKeyRef:",
        "Decrypting secret /builds/cix/clusters/africa/sso/secrets-main.yaml",
        "master, portal-govone-africa-portal, Deployment (apps) has changed:",
        "UPDATED RELEASES:",
        "https://gitlab.govone.digital/cix/apps/admin-api/-/jobs/324417",
        "tokens processados: 42",
    ],
)
def test_texto_comum_nao_e_redigido(text):
    out, count = redact([text])
    assert out == [text]
    assert count == 0


def test_redacao_e_idempotente():
    first, n1 = redact(["DB_PASSWORD=abc", "amqp://u:p@h"])
    second, n2 = redact(first)
    assert second == first
    assert n1 == 2 and n2 == 0
