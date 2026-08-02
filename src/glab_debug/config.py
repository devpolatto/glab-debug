"""Configuração global do glab-debug.

Precedência (da maior para a menor):

1. flags da linha de comando (aplicadas em `Settings.merge_cli`)
2. variáveis de ambiente `GLAB_DEBUG_*` (e `GITLAB_HOST`, que o próprio `glab` usa)
3. arquivo `~/.config/glab-debug/config.toml`
4. defaults declarados aqui
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

OutputFormat = Literal["table", "compact", "markdown", "json"]

CONFIG_DIR = Path(
    os.environ.get("GLAB_DEBUG_CONFIG_DIR")
    or Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "glab-debug"
).expanduser()

CONFIG_FILE = CONFIG_DIR / "config.toml"


class Settings(BaseSettings):
    """Configurações globais da aplicação."""

    model_config = SettingsConfigDict(
        env_prefix="GLAB_DEBUG_",
        toml_file=CONFIG_FILE,
        extra="ignore",
        # Sem isto, campos com `validation_alias` só seriam lidos do TOML pelo nome do
        # alias (`GLAB_DEBUG_HOST`), nunca pelo nome do campo (`host`).
        populate_by_name=True,
    )

    host: str = Field(
        default="gitlab.domain.local",
        validation_alias=AliasChoices("GLAB_DEBUG_HOST", "GITLAB_HOST"),
        description="Hostname da instância GitLab alvo.",
    )
    project: str | None = Field(
        default=None,
        description=(
            "Projeto padrão (path com namespace, ID numérico ou URL). "
            "Quando ausente, é inferido do remote git do diretório atual."
        ),
    )
    output_format: OutputFormat = Field(
        default="table",
        validation_alias=AliasChoices("GLAB_DEBUG_OUTPUT_FORMAT", "GLAB_DEBUG_FORMAT"),
        description="Formato de saída padrão.",
    )
    glab_bin: str = Field(default="glab", description="Caminho do executável glab.")
    timeout: int = Field(default=60, ge=1, description="Timeout por chamada à API, em segundos.")
    per_page: int = Field(default=100, ge=1, le=100, description="Itens por página na API.")
    max_pages: int = Field(default=50, ge=1, description="Teto de páginas por consulta paginada.")
    color: bool | None = Field(
        default=None,
        description="Força cor ligada/desligada. None = detecta TTY e respeita NO_COLOR.",
    )
    debug: bool = Field(default=False, description="Loga na stderr cada chamada ao glab.")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
        )

    def merge_cli(self, **overrides: Any) -> "Settings":
        """Devolve uma cópia com as flags de CLI aplicadas por cima (ignora `None`)."""
        given = {k: v for k, v in overrides.items() if v is not None}
        return self.model_copy(update=given) if given else self


def load_settings() -> Settings:
    return Settings()
