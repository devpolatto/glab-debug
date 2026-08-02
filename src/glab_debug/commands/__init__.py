"""Grupos de comandos do glab-debug.

Cada módulo aqui expõe `register(subparsers)`, que cria o grupo raiz e seus
subcomandos. Para adicionar um grupo novo, escreva o módulo e liste-o em `GROUPS`.
"""

from __future__ import annotations

from . import pipeline

GROUPS = (pipeline,)

__all__ = ["GROUPS"]
