"""Primitivas de apresentação: cor, duração, tabela e barra de tempo."""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "gray": "90",
}

_STATUS_STYLE = {
    "success": "green",
    "failed": "red",
    "running": "blue",
    "pending": "yellow",
    "created": "gray",
    "waiting_for_resource": "yellow",
    "preparing": "yellow",
    "manual": "cyan",
    "scheduled": "cyan",
    "canceled": "magenta",
    "canceling": "magenta",
    "skipped": "gray",
}

_LEVEL_STYLE = {"erro": "red", "aviso": "yellow", "info": "cyan"}
_LEVEL_MARK = {"erro": "✖", "aviso": "▲", "info": "•"}

_color_enabled = True


def configure_color(force: bool | None) -> None:
    global _color_enabled
    if force is not None:
        _color_enabled = force
        return
    _color_enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, *styles: str) -> str:
    if not _color_enabled or not styles:
        return text
    codes = ";".join(_CODES[s] for s in styles if s in _CODES)
    return f"\x1b[{codes}m{text}\x1b[0m" if codes else text


def status_style(status: str) -> str:
    return _STATUS_STYLE.get(status, "")


def paint_status(status: str) -> str:
    style = status_style(status)
    return paint(status, style) if style else status


def level_mark(level: str) -> str:
    return paint(_LEVEL_MARK.get(level, "•"), _LEVEL_STYLE.get(level, ""))


def visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def term_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


# --------------------------------------------------------------------------- #
# Números e datas
# --------------------------------------------------------------------------- #


def duration(seconds: float | None, empty: str = "—") -> str:
    """Formata segundos de forma legível, mantendo precisão nos valores curtos."""
    if seconds is None:
        return empty
    seconds = max(float(seconds), 0.0)
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m{rest:04.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h{minutes:02d}m{rest:04.1f}s"


def seconds(value: float | None, empty: str = "—") -> str:
    return empty if value is None else f"{value:.1f}"


def percent(fraction: float | None, empty: str = "—") -> str:
    return empty if fraction is None else f"{fraction * 100:.1f}%"


def clock(moment: datetime | None, empty: str = "—") -> str:
    return empty if moment is None else moment.astimezone().strftime("%d/%m %H:%M:%S")


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 1)] + "…"


# --------------------------------------------------------------------------- #
# Tabela
# --------------------------------------------------------------------------- #


class Table:
    """Tabela simples que renderiza tanto para terminal quanto para Markdown."""

    def __init__(self, headers: list[str], aligns: str | None = None) -> None:
        self.headers = headers
        self.aligns = aligns or "l" * len(headers)
        self.rows: list[list[str]] = []

    def add(self, *cells: str) -> None:
        self.rows.append([str(c) for c in cells])

    def _widths(self) -> list[int]:
        widths = [visible_len(h) for h in self.headers]
        for row in self.rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], visible_len(cell))
        return widths

    def _pad(self, cell: str, width: int, align: str) -> str:
        gap = width - visible_len(cell)
        if gap <= 0:
            return cell
        return " " * gap + cell if align == "r" else cell + " " * gap

    def render(self) -> str:
        if not self.rows:
            return paint("  (nenhum job)", "dim")
        widths = self._widths()
        out = [
            "  "
            + "  ".join(
                paint(self._pad(h, widths[i], self.aligns[i]), "bold")
                for i, h in enumerate(self.headers)
            ),
            "  " + "  ".join(paint("─" * w, "dim") for w in widths),
        ]
        for row in self.rows:
            out.append(
                "  " + "  ".join(self._pad(c, widths[i], self.aligns[i]) for i, c in enumerate(row))
            )
        return "\n".join(out)

    def render_markdown(self) -> str:
        if not self.rows:
            return "_nenhum job_"
        sep = {"l": ":---", "r": "---:", "c": ":---:"}
        out = [
            "| " + " | ".join(self.headers) + " |",
            "| " + " | ".join(sep.get(a, ":---") for a in self.aligns) + " |",
        ]
        for row in self.rows:
            out.append("| " + " | ".join(strip_ansi(c).replace("|", "\\|") for c in row) + " |")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# Linha do tempo
# --------------------------------------------------------------------------- #


def timeline_bar(offset: float, length: float, total: float, width: int) -> tuple[str, int]:
    """Devolve a barra ASCII de um job e a coluna em que ela começa."""
    if total <= 0 or width <= 0:
        return "", 0
    scale = width / total
    start = int(offset * scale)
    size = max(int(round(length * scale)), 1)
    start = min(start, max(width - 1, 0))
    size = min(size, width - start)
    return " " * start + "█" * size, start
