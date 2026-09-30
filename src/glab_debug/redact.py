"""Redação de credenciais em texto de log.

Todo texto de trace que sai da ferramenta passa por aqui, e não há flag para desligar:
o trace de um job de deploy roda sobre secrets decifrados, e o `helm diff` imprime em
claro o `env` dos Deployments. Quem precisa do valor cru abre o job na interface do
GitLab.

A redação é por linha, com um único estado: a linha anterior ter sido um
`name: <SENSÍVEL>` do formato de `env` do Kubernetes, cujo valor vem na linha seguinte.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MASK = "<redacted>"

#: Trecho que marca um nome de variável/chave como sensível.
_SENSITIVE = r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|DSN)"

#: Chaves que contêm um termo sensível mas cujo valor é só uma referência
#: (`secretName: portal-tls`, `tokenPath: /var/run/...`).
_REFERENCE_SUFFIX = re.compile(r"(?:name|ref|refs|path|file|key_?id_?ref)$", re.IGNORECASE)

_SENSITIVE_NAME = re.compile(_SENSITIVE, re.IGNORECASE)

# `- name: DB_PASSWORD` (a linha seguinte traz `value: ...`)
_ENV_NAME = re.compile(r"^.*?\bname:\s*[\"']?(?P<name>[A-Za-z0-9_.-]+)[\"']?\s*$")
_ENV_VALUE = re.compile(r"^(?P<pre>.*?\bvalue:\s*)(?P<val>\S.*)$")

# `SECRET_KEY=abc`, `password: abc`, `"api_key": "abc"`, `PRIVATE-TOKEN: abc`
_ASSIGNMENT = re.compile(
    r"(?P<key>[\"']?\b[A-Za-z0-9_.-]*" + _SENSITIVE + r"[A-Za-z0-9_.-]*[\"']?)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)",
    re.IGNORECASE,
)

# `amqp://user:senha@host`, `redis://:senha@host`
_URL_USERINFO = re.compile(r"(?P<pre>\b[a-z][a-z0-9+.-]*://[^\s:/@]*:)(?P<pw>[^\s@/]+)(?P<at>@)", re.IGNORECASE)

_AUTH_HEADER = re.compile(r"(?P<pre>\bAuthorization:\s*(?:Bearer|Basic|Token)\s+)(?P<val>\S+)", re.IGNORECASE)

# Formatos de credencial reconhecíveis pelo próprio valor.
_LITERALS = (
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted:aws-key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<redacted:jwt>"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "<redacted:gitlab-token>"),
)


def _is_sensitive_name(name: str) -> bool:
    bare = name.strip("\"'")
    return bool(_SENSITIVE_NAME.search(bare)) and not _REFERENCE_SUFFIX.search(bare)


def _already_masked(value: str) -> bool:
    return value.strip("\"'").startswith("<redacted")


class Redactor:
    """Aplica as regras linha a linha e conta quantos trechos foram cortados."""

    def __init__(self) -> None:
        self.count = 0
        self._pending_env_value = False

    def line(self, text: str) -> str:
        pending, self._pending_env_value = self._pending_env_value, False

        if pending:
            match = _ENV_VALUE.match(text)
            if match and not _already_masked(match["val"]):
                self.count += 1
                return match["pre"] + MASK

        env = _ENV_NAME.match(text)
        if env and _is_sensitive_name(env["name"]):
            self._pending_env_value = True

        text = _URL_USERINFO.sub(self._sub_url, text)
        text = _AUTH_HEADER.sub(self._sub_header, text)
        text = _ASSIGNMENT.sub(self._sub_assignment, text)
        for pattern, mask in _LITERALS:
            text, n = pattern.subn(mask, text)
            self.count += n
        return text

    def lines(self, lines: Iterable[str]) -> list[str]:
        return [self.line(text) for text in lines]

    # ------------------------------------------------------------ substituições

    def _sub_url(self, match: re.Match[str]) -> str:
        if _already_masked(match["pw"]):
            return match[0]
        self.count += 1
        return f"{match['pre']}{MASK}{match['at']}"

    def _sub_header(self, match: re.Match[str]) -> str:
        if _already_masked(match["val"]):
            return match[0]
        self.count += 1
        return match["pre"] + MASK

    def _sub_assignment(self, match: re.Match[str]) -> str:
        if not _is_sensitive_name(match["key"]) or _already_masked(match["val"]):
            return match[0]
        self.count += 1
        return f"{match['key']}{match['sep']}{MASK}"


def redact(lines: Iterable[str]) -> tuple[list[str], int]:
    """Atalho: redige uma sequência de linhas e devolve também o total cortado."""
    redactor = Redactor()
    out = redactor.lines(lines)
    return out, redactor.count
