"""Precedência de configuração: flags de CLI > env > `config.toml` > defaults.

Regressão: campos com `validation_alias` (`host`, `output_format`) eram ignorados
no TOML, porque o `TomlConfigSettingsSource` só procurava pelos nomes dos aliases
(`GLAB_DEBUG_HOST`, `GITLAB_HOST`) e nunca pelo nome do campo.
"""

import importlib
import os

import pytest
from pydantic import ValidationError

from glab_debug import config as config_module

TOML_COMPLETO = """
host = "gitlab.govone.digital"
output_format = "compact"
timeout = 99
per_page = 42
"""


@pytest.fixture
def carregar(tmp_path, monkeypatch):
    """Devolve um `load_settings` isolado: TOML em `tmp_path` e ambiente limpo.

    O `reload` é necessário porque `CONFIG_FILE` e o `model_config` da classe são
    resolvidos no import do módulo.
    """
    for var in list(os.environ):
        if var.startswith("GLAB_DEBUG_") or var == "GITLAB_HOST":
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GLAB_DEBUG_CONFIG_DIR", str(tmp_path))

    def build(toml: str | None = None, **env: str):
        if toml is not None:
            (tmp_path / "config.toml").write_text(toml)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(config_module).load_settings()

    yield build

    monkeypatch.undo()
    importlib.reload(config_module)


def test_host_vem_do_toml(carregar):
    assert carregar(TOML_COMPLETO).host == "gitlab.govone.digital"


def test_output_format_vem_do_toml(carregar):
    assert carregar(TOML_COMPLETO).output_format == "compact"


def test_campos_sem_alias_vem_do_toml(carregar):
    settings = carregar(TOML_COMPLETO)
    assert (settings.timeout, settings.per_page) == (99, 42)


def test_defaults_quando_nao_ha_arquivo(carregar):
    settings = carregar()
    assert settings.host == "gitlab.domain.local"
    assert settings.output_format == "table"
    assert settings.project is None


@pytest.mark.parametrize("var", ["GLAB_DEBUG_HOST", "GITLAB_HOST"])
def test_env_sobrescreve_toml(carregar, var):
    assert carregar(TOML_COMPLETO, **{var: "env.exemplo"}).host == "env.exemplo"


def test_glab_debug_host_tem_precedencia_sobre_gitlab_host(carregar):
    settings = carregar(TOML_COMPLETO, GLAB_DEBUG_HOST="proprio.exemplo", GITLAB_HOST="generico.exemplo")
    assert settings.host == "proprio.exemplo"


def test_alias_de_formato_abreviado(carregar):
    assert carregar(TOML_COMPLETO, GLAB_DEBUG_FORMAT="json").output_format == "json"


def test_flag_de_cli_sobrescreve_env_e_toml(carregar):
    settings = carregar(TOML_COMPLETO, GITLAB_HOST="env.exemplo")
    assert settings.merge_cli(host="cli.exemplo").host == "cli.exemplo"


def test_merge_cli_ignora_none(carregar):
    settings = carregar(TOML_COMPLETO)
    merged = settings.merge_cli(host=None, output_format=None, timeout=5)
    assert merged.host == "gitlab.govone.digital"
    assert merged.output_format == "compact"
    assert merged.timeout == 5


def test_valor_invalido_no_toml_falha(carregar):
    with pytest.raises(ValidationError):
        carregar('output_format = "yaml"\n')
