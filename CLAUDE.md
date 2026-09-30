# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Comandos

```bash
uv sync                                   # cria .venv com deps + grupo dev
uv run pytest                             # suíte inteira (~100 testes, ~0.2s)
uv run pytest tests/test_analysis.py      # um arquivo
uv run pytest -k union_seconds            # um teste por nome
uv run glab-debug pipeline monitor 58185  # roda a CLI do repositório

uv tool install --editable .              # instala em ~/.local/bin apontando para o fonte
```

Não há linter nem formatter configurado no projeto.

Os testes não fazem rede: `test_analysis.py` monta payloads pydantic à mão e exporta
`PROJECT`, `make_job`, `make_pipeline`, que `test_render.py` importa direto (`from
test_analysis import ...`). Ao criar fixtures novas de pipeline, reaproveite essas.

## Arquitetura

Fluxo único, em quatro camadas que não se cruzam:

```
glab api (subprocess) → payloads pydantic → relatório derivado → renderizador
     gitlab.py             models.py           analysis.py        commands/*.py + fmt.py
```

- **`gitlab.py`** — nenhuma chamada HTTP direta e nenhum token: tudo passa por `subprocess`
  sobre a CLI `glab`, que já está autenticada na máquina. `GITLAB_HOST` é injetado no env de
  cada chamada. A paginação é manual (`get_all`) porque o `--paginate` do `glab` não devolve
  um JSON único. `project_ref` normaliza ID/path/URL para o formato da rota (`%2F`).
- **`models.py`** — payloads da API herdam `_Payload` com `extra="ignore"`: campo novo do
  GitLab não pode quebrar a ferramenta. Os modelos de relatório (`JobReport`, `StageReport`,
  `Timing`, `PipelineReport`) são o contrato entre análise e apresentação — e também o
  formato `json` da CLI, que é literalmente `report.model_dump_json()`.
- **`analysis.py`** — onde todo número é derivado, e o único lugar que deve derivar número.
  O ponto não óbvio é `_union_seconds`: mescla os intervalos de execução dos jobs para
  separar *busy* (algo rodava) de *idle* (nada rodava), e daí sai `parallelism = jobs_sum /
  busy`. Os limiares de alerta são constantes no topo do módulo (`QUEUE_WARN_SECONDS`,
  `DOMINANT_SHARE`, `IDLE_WARN_SHARE`). Quando a API não expõe `finished_at`, o fim é
  inferido do último job concluído e `Timing.finished_estimated` marca isso.
- **`fmt.py`** — primitivas de apresentação sem conhecimento de domínio: cor, `duration`,
  `Table` (renderiza texto *e* markdown), `timeline_bar`. Largura de coluna usa
  `visible_len`, que ignora ANSI.

### Trace: limpeza, redação e helmfile

Os comandos do grupo `job` leem o trace cru, que não é JSON (`GitLabClient.get_text`). O
fluxo é sempre `trace.clean` → `redact.redact` → recorte (`tail`/`grep`) ou
`helmfile.parse` → renderizador. Três módulos puros, testáveis sem rede:

- **`trace.py`** — o prefixo do runner é `<timestamp> <stream><O|E>` seguido de espaço ou
  de `+`, que marca continuação da linha anterior. Os marcadores
  `section_start|end:<epoch>:<nome>` podem aparecer no meio de uma linha física, e o `\r`
  de barra de progresso mantém só o último trecho. `MAX_LINES` é o teto de saída.
- **`redact.py`** — **invariante de segurança: nenhum texto de trace sai sem passar por
  aqui, e não existe flag para desligar.** O único estado é "a linha anterior foi `name:
  <SENSÍVEL>`": ele continua ativo por linhas `value:` seguidas, porque o helm-diff mostra
  um valor alterado em duas linhas (`-`/`+`). `tests/test_job.py` prova ponta a ponta que
  nenhum valor sintético vaza em nenhum dos quatro formatos. Ao adicionar um comando que
  imprime trace, estenda esse teste.
- **`helmfile.py`** — as fronteiras de um bloco de diff (`_is_boundary`) são o ponto frágil:
  linha nova do helmfile que não for reconhecida como fronteira vai parar no corpo do diff
  do objeto anterior.

### Grupos de comandos

`cli.py` monta `grupo → subcomando` com argparse e delega para `args.handler(args,
settings)`. Cada módulo em `commands/` expõe `register(subparsers)` e é listado em
`commands/__init__.py::GROUPS` — adicionar um grupo é criar o módulo e incluí-lo na tupla;
config, cor e formatação vêm de graça.

### Os quatro formatos

Cada comando implementa `table`, `compact`, `markdown` e `json` (ver o dict `renderers` em
`commands/pipeline.py::run_monitor`). O `compact` existe porque a saída costuma ser lida por
um LLM, e ele é ~3× mais barato em tokens que o `table`. Invariante que o `test_render.py`
protege: **`compact` não perde nenhum número em relação ao `table`** — o que ele corta é
decoração (alinhamento, separadores), as seções deriváveis das linhas de job (stages e linha
do tempo) e os alertas de nível `info`. Ao adicionar métrica nova, ela precisa aparecer nos
quatro.

A cor é desligada à força quando `output_format != "table"` (`cli.py`), para que `compact`,
`markdown` e `json` sejam sempre parseáveis.

### Códigos de saída

`0` ok · `1` algum alerta de nível `erro` (job falhou) · `2` erro de execução (`GlabError`)
ou config inválida · `3` `pipeline status --wait` estourou o timeout (falha prevalece) ·
`130` interrupção. São contrato para uso em script.

## Configuração

Precedência: **flags de CLI → env `GLAB_DEBUG_*` → `~/.config/glab-debug/config.toml` →
defaults**. As flags entram por `Settings.merge_cli`, que ignora `None` — por isso as flags
globais em `cli.py` usam `default=None`, e não `False`.

Duas armadilhas já corrigidas, com teste de regressão em `tests/test_config.py`:

- `populate_by_name=True` no `model_config` é obrigatório: sem ele, campos com
  `validation_alias` (`host`, `output_format`) só seriam lidos do TOML pelo nome do alias
  (`GLAB_DEBUG_HOST`), nunca pelo nome do campo.
- `CONFIG_DIR`/`CONFIG_FILE` e o `model_config` são resolvidos **no import** do módulo. Todo
  teste que mexe em `GLAB_DEBUG_CONFIG_DIR` precisa de `importlib.reload(config_module)`.

## Convenções

- Texto voltado ao usuário — help do argparse, mensagens de erro, alertas, docstrings — em
  português. Identificadores em inglês.
- `Alert.level` usa os literais `"erro"`, `"aviso"`, `"info"`; o código de saída depende
  disso.
- `bin/` e `glab_debug/` na raiz são diretórios vazios remanescentes; o pacote real é
  `src/glab_debug/`.
