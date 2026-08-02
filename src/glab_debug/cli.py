"""Ponto de entrada da CLI: `glab-debug <grupo> <subcomando> [opções]`."""

from __future__ import annotations

import argparse
import sys

from . import __version__, fmt
from .commands import GROUPS
from .config import Settings, load_settings
from .gitlab import GlabError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glab-debug",
        description="Utilitário de diagnóstico do GitLab construído sobre a CLI glab.",
        epilog="Exemplo: glab-debug pipeline monitor --project cix/apps/admin-api 58185",
    )
    parser.add_argument("--version", action="version", version=f"glab-debug {__version__}")

    common = parser.add_argument_group("configuração global")
    common.add_argument("--host", help="Hostname do GitLab alvo (sobrescreve config e ambiente).")
    common.add_argument(
        "-f",
        "--format",
        dest="output_format",
        choices=("table", "compact", "markdown", "json"),
        help="Formato de saída. `compact` é denso, feito para consumo por LLM/script.",
    )
    color = common.add_mutually_exclusive_group()
    color.add_argument("--color", dest="color", action="store_true", default=None, help="Força cor.")
    color.add_argument("--no-color", dest="color", action="store_false", help="Desliga cor.")
    common.add_argument("--timeout", type=int, help="Timeout por chamada à API, em segundos.")
    common.add_argument("--debug", action="store_true", default=None, help="Loga as chamadas ao glab.")

    subparsers = parser.add_subparsers(dest="group", metavar="<grupo>", required=True)
    for module in GROUPS:
        module.register(subparsers)
    return parser


def resolve_settings(args: argparse.Namespace) -> Settings:
    return load_settings().merge_cli(
        host=args.host,
        output_format=args.output_format,
        color=args.color,
        timeout=args.timeout,
        debug=args.debug,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = resolve_settings(args)
    except Exception as exc:  # configuração inválida (toml/env fora do schema)
        print(f"glab-debug: configuração inválida: {exc}", file=sys.stderr)
        return 2

    fmt.configure_color(False if settings.output_format != "table" else settings.color)

    try:
        return args.handler(args, settings)
    except GlabError as exc:
        print(f"glab-debug: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("glab-debug: interrompido.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
