"""Parse do trace de um job de `helmfile apply|diff|sync`.

Recebe as linhas já limpas (`trace.clean`) e extrai o que um deploy GitOps precisa
responder: quais releases foram comparados, quais objetos mudaram, o que o helm
atualizou de fato e como o job terminou. O corpo de cada diff é guardado à parte e só
sai quando pedido, sempre redigido por quem renderiza.

Formato de referência: helmfile v0.171 + helm-diff, como no job 366779 do
`cix-app-helmfiles`.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

_COMPARING = re.compile(r"^Comparing release=(?P<name>[^,\s]+), chart=(?P<chart>[^,\s]+), namespace=(?P<ns>\S+)")
_CHANGE = re.compile(
    r"^(?P<ns>[^,\s]+), (?P<name>[^,\s]+), (?P<kind>[A-Za-z0-9]+)(?: \((?P<group>[^)]*)\))? "
    r"has (?P<what>changed|been added|been removed):?\s*$"
)
_TABLE_TITLE = re.compile(r"^(?P<kind>UPDATED|DELETED|FAILED) RELEASES:\s*$")
_RESULT = re.compile(r"^(?:ERROR: )?Job (?P<result>succeeded|failed)\b")
_ERROR = re.compile(r"^(?:Error:|ERROR:|err:|in \S+helmfile\S*: )|UPGRADE FAILED|has no deployed releases")

_WHAT = {"changed": "changed", "been added": "added", "been removed": "removed"}


class Change(BaseModel):
    namespace: str
    name: str
    kind: str
    group: str | None = None
    action: str = Field(description="changed | added | removed")
    diff: list[str] = Field(default_factory=list, description="Corpo do diff, cru — redigir antes de exibir.")


class ReleaseRow(BaseModel):
    table: str = Field(description="UPDATED | DELETED | FAILED")
    name: str
    namespace: str | None = None
    chart: str | None = None
    version: str | None = None
    duration: str | None = None


class HelmfileSummary(BaseModel):
    compared: list[str]
    changes: list[Change]
    releases: list[ReleaseRow]
    result: str | None = Field(default=None, description="succeeded | failed, da última linha do runner.")
    errors: list[str] = Field(default_factory=list)

    @property
    def no_changes(self) -> bool:
        return bool(self.compared) and not self.changes and not self.releases


def _is_boundary(text: str) -> bool:
    return bool(
        _COMPARING.match(text)
        or _CHANGE.match(text)
        or _TABLE_TITLE.match(text)
        or _RESULT.match(text)
        or text.startswith(
            (
                "Pulling ", "Decrypting secret ", "Adding repo ", "Building dependency",
                "Listing releases", "Upgrading release=", "Deleting ", "Affected releases are:",
            )
        )
        or (text.startswith("Release \"") and "has been" in text)
    )


def _parse_table(lines: list[str], start: int, kind: str) -> tuple[list[ReleaseRow], int]:
    """Lê a tabela que segue `UPDATED RELEASES:` e devolve as linhas e onde ela acabou."""
    i = start
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return [], i
    header = [h.lower() for h in lines[i].split()]
    rows: list[ReleaseRow] = []
    i += 1
    while i < len(lines) and lines[i].strip() and not _is_boundary(lines[i]):
        cells = dict(zip(header, lines[i].split()))
        rows.append(
            ReleaseRow(
                table=kind,
                name=cells.get("name", lines[i].split()[0]),
                namespace=cells.get("namespace"),
                chart=cells.get("chart"),
                version=cells.get("version"),
                duration=cells.get("duration"),
            )
        )
        i += 1
    return rows, i


def parse(lines: list[str]) -> HelmfileSummary:
    compared: list[str] = []
    changes: list[Change] = []
    releases: list[ReleaseRow] = []
    errors: list[str] = []
    result: str | None = None
    current: Change | None = None

    i = 0
    while i < len(lines):
        text = lines[i]

        if match := _COMPARING.match(text):
            current = None
            if match["name"] not in compared:
                compared.append(match["name"])
        elif match := _CHANGE.match(text):
            current = Change(
                namespace=match["ns"],
                name=match["name"],
                kind=match["kind"],
                group=match["group"],
                action=_WHAT[match["what"]],
            )
            changes.append(current)
        elif match := _TABLE_TITLE.match(text):
            current = None
            rows, i = _parse_table(lines, i + 1, match["kind"])
            releases += rows
            continue
        elif match := _RESULT.match(text):
            current = None
            result = match["result"]
        elif _is_boundary(text):
            current = None
        elif current is not None:
            current.diff.append(text)

        if _ERROR.search(text) and len(errors) < 10:
            errors.append(text)
        i += 1

    return HelmfileSummary(compared=compared, changes=changes, releases=releases, result=result, errors=errors)
