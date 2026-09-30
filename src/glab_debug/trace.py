"""Limpeza do trace cru de um job do GitLab.

O trace da API não é texto legível. Cada linha vem com um prefixo do runner
(`2026-09-30T19:38:19.213893Z 00O `), onde `O`/`E` é o stream e um `+` no lugar do
espaço marca continuação da linha anterior. O texto carrega códigos ANSI, `\\r` de barra
de progresso e os marcadores de seção do GitLab
(`section_start:<epoch>:<nome>[opções]\\r\\x1b[0K`). Aqui tudo isso vira linhas limpas e
uma lista de seções com duração.

Traces de runners antigos, sem o prefixo de timestamp, também são aceitos: a linha
inteira é tratada como texto.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel

#: Teto de linhas em qualquer saída de log, independentemente do que for pedido.
MAX_LINES = 400

_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z [0-9a-f]{2}[OE](?P<cont>[+ ])")
_SECTION = re.compile(r"section_(?P<kind>start|end):(?P<epoch>\d+):(?P<name>[A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\r?(?:\x1b\[0K)?")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Section(BaseModel):
    name: str
    start_line: int
    end_line: int | None = None
    duration: float | None = None


class CleanTrace(BaseModel):
    lines: list[str]
    sections: list[Section]


def _join_continuations(raw: str) -> list[str]:
    """Remove o prefixo do runner e junta as continuações (`+`) à linha anterior."""
    joined: list[str] = []
    for line in raw.split("\n"):
        match = _PREFIX.match(line)
        if not match:
            joined.append(line)
            continue
        body = line[match.end():]
        if match["cont"] == "+" and joined:
            joined[-1] += body
        else:
            joined.append(body)
    return joined


def _visible(text: str) -> str:
    """O que um terminal mostraria: último trecho após `\\r` e sem ANSI."""
    text = _ANSI.sub("", text)
    if "\r" in text:
        parts = [p for p in text.split("\r") if p.strip()]
        text = parts[-1] if parts else ""
    return text.rstrip()


def clean(raw: str) -> CleanTrace:
    lines: list[str] = []
    sections: list[Section] = []
    open_sections: dict[str, tuple[Section, int]] = {}

    for physical in _join_continuations(raw):
        cursor = 0
        pieces: list[str] = []
        for marker in _SECTION.finditer(physical):
            pieces.append(physical[cursor:marker.start()])
            cursor = marker.end()
            name, epoch = marker["name"], int(marker["epoch"])
            if marker["kind"] == "start":
                section = Section(name=name, start_line=len(lines) + 1)
                sections.append(section)
                open_sections[name] = (section, epoch)
            elif name in open_sections:
                section, started = open_sections.pop(name)
                section.end_line = len(lines)
                section.duration = float(max(epoch - started, 0))
        pieces.append(physical[cursor:])

        text = _visible("".join(pieces))
        if text:
            lines.append(text)

    return CleanTrace(lines=lines, sections=sections)


# --------------------------------------------------------------------------- #
# Recortes
# --------------------------------------------------------------------------- #


class Excerpt(BaseModel):
    """Trecho selecionado do log: pares (nº da linha, texto) e o que ficou de fora."""

    rows: list[tuple[int, str]]
    total_lines: int
    truncated: int = 0
    matches: int | None = None


def _cap(rows: list[tuple[int, str]], limit: int) -> tuple[list[tuple[int, str]], int]:
    limit = min(limit, MAX_LINES)
    if len(rows) <= limit:
        return rows, 0
    return rows[-limit:], len(rows) - limit


def tail(lines: list[str], n: int) -> Excerpt:
    start = max(len(lines) - n, 0)
    rows = [(i + 1, lines[i]) for i in range(start, len(lines))]
    rows, cut = _cap(rows, n)
    return Excerpt(rows=rows, total_lines=len(lines), truncated=cut)


def grep(lines: list[str], pattern: str, context: int = 3, limit: int = MAX_LINES) -> Excerpt:
    """Linhas que batem com `pattern` e `context` linhas em volta; janelas sobrepostas se fundem."""
    regex = re.compile(pattern)
    hits = [i for i, text in enumerate(lines) if regex.search(text)]
    keep: set[int] = set()
    for i in hits:
        keep.update(range(max(i - context, 0), min(i + context + 1, len(lines))))
    rows = [(i + 1, lines[i]) for i in sorted(keep)]
    if len(rows) > min(limit, MAX_LINES):
        cut = len(rows) - min(limit, MAX_LINES)
        rows = rows[: min(limit, MAX_LINES)]  # no grep, os primeiros matches importam mais
    else:
        cut = 0
    return Excerpt(rows=rows, total_lines=len(lines), truncated=cut, matches=len(hits))


def gaps(rows: Iterable[tuple[int, str]]) -> list[tuple[int, str] | None]:
    """Intercala `None` onde há salto de numeração, para o renderizador marcar `…`."""
    out: list[tuple[int, str] | None] = []
    previous: int | None = None
    for number, text in rows:
        if previous is not None and number != previous + 1:
            out.append(None)
        out.append((number, text))
        previous = number
    return out
