# glab-debug

[![ci](https://github.com/devpolatto/glab-debug/actions/workflows/ci.yml/badge.svg?branch=development&event=push)](https://github.com/devpolatto/glab-debug/actions/workflows/ci.yml?query=branch%3Adevelopment+event%3Apush)

Utilitário de diagnóstico do GitLab construído **em cima da CLI `glab`** — nada de token
próprio, nada de config de autenticação: se o `glab` está logado, o `glab-debug` funciona.

Nasceu de uma constatação prática: investigar por que uma pipeline demora sempre acaba
sendo a mesma sequência de `glab api` + `jq` + conta de cabeça. Isto empacota essa rotina.

## Instalação

```bash
uv tool install --editable ~/Playground/glab-debug
```

O `--editable` faz o comando apontar para o código-fonte, então editar o repositório já
muda o comportamento — sem reinstalar. O executável vai para `~/.local/bin/glab-debug`.

Para desenvolver dentro do repositório:

```bash
uv sync          # cria o .venv com as dependências
uv run glab-debug --version
uv run pytest
```

## Uso

```
glab-debug <grupo> <subcomando> [opções] [args]
```

### `pipeline monitor`

Quebra uma execução de pipeline job a job.

```bash
glab-debug pipeline monitor --project cix/apps/admin-api 58185
```

O que sai:

| seção | conteúdo |
|:---|:---|
| **Tempos** | relógio de parede, `duration` da API, soma dos jobs, tempo em fila, tempo executando × ocioso, e o fator de paralelismo |
| **Jobs** | stage, nome, status, fila, duração (formatada e em segundos), % do total e runner |
| **Stages** | agregação por stage: nº de jobs, soma e *span* (do primeiro início ao último fim) |
| **Runners** | mapa `id → descrição`, para flagrar job rodando em runner diferente do esperado |
| **Linha do tempo** | gráfico ASCII proporcional, que mostra o caminho crítico e onde há paralelismo |
| **Alertas** | jobs falhos, reexecuções, fila alta, job dominante, tempo ocioso |

Três números que a interface do GitLab não dá de graça e que são o ponto da ferramenta:

- **soma dos jobs × relógio de parede** — a diferença é overhead de agendamento, não build.
- **executando × ocioso** — quanto do pipeline não tinha *nenhum* job rodando (fila,
  aprovação manual, runner ocupado).
- **paralelismo** — `soma dos jobs ÷ tempo executando`. `1.00×` significa execução
  estritamente serial, e portanto que cortar o job mais lento corta o pipeline inteiro.

Opções:

| flag | efeito |
|:---|:---|
| `-p`, `--project` | path com namespace, ID numérico ou URL. Se omitido, infere do remote git ou da config |
| `--retried` | inclui tentativas descartadas de jobs reexecutados |
| `--sort` | `duration` (default), `start`, `stage`, `name` |
| `--top N` | só os N jobs mais longos; o resto vira uma linha de resumo |
| `--no-timeline`, `--no-stages` | omitem as respectivas seções |
| `-f`, `--format` | `table` (default), `compact`, `markdown`, `json` |

O código de saída é `1` quando algum job falhou e `2` em erro de execução — dá para usar
em script.

### `pipeline status`

Em que pé está um pipeline e quais jobs importam. Responde "o deploy do MR passou?" sem
listar os jobs `manual`/`created`/`skipped`, que num pipeline GitOps são a maioria.

```bash
glab-debug -f compact pipeline status -p cix/devops/ci-deployments/cix-app-helmfiles --mr 50
```

```
pipeline 67511 cix/devops/ci-deployments/cix-app-helmfiles ref=main sha=6f7a024d status=manual source=push via=merge_commit mr=!50 mr_state=merged
url https://gitlab.govone.digital/cix/devops/ci-deployments/cix-app-helmfiles/-/pipelines/67511
counts created=7 manual=7 skipped=1 success=1
jobs stage/name status dur reason id
deploy/deploy:govone-v2-africa-prd success 86.8 - 366779
+15 jobs fora da lista
```

| alvo | resolve para |
|:---|:---|
| `<pipeline-id>` | o próprio pipeline |
| `--sha SHA` | o pipeline mais recente do commit (aceita SHA curto) |
| `--ref REF` | o último pipeline do branch/tag |
| `--mr IID` | **mergeado**: o pipeline do merge commit no branch alvo (`via=merge_commit`), que é o do deploy. **Aberto**: o pipeline do MR (`via=head_pipeline`), com aviso |

| flag | efeito |
|:---|:---|
| `--job GLOB` | filtra por nome (`fnmatch`, repetível); jobs filtrados aparecem em qualquer estado |
| `--all` | lista todos os jobs |
| `--wait` | espera o pipeline — ou só os jobs filtrados — sair de `created/pending/running…`. `manual` conta como final |
| `--interval`, `--wait-timeout` | polling do `--wait` (default 15s e 1800s) |

Saída: `0` ok (inclui `manual`) · `1` job falhou sem `allow_failure`, ou pipeline `failed`
sem job falho (erro de criação: YAML, rules, include) · `2` erro de execução · `3`
`--wait-timeout` estourado. Falha prevalece sobre timeout.

### `job log`

Trecho do log de um job, limpo (sem prefixo do runner, ANSI, `\r` nem marcadores de
seção) e **redigido**. Teto de 400 linhas por job.

```bash
glab-debug -f compact job log -p 621 324417 --tail 40
glab-debug -f compact job log -p 621 --pipeline 58087 --failed --grep 'error|Error' --context 2
glab-debug -f compact job log -p 621 324417 --sections      # seções com duração, sem conteúdo
```

Seleção: `<job-id>`, ou `--pipeline ID` com `--job GLOB` e/ou `--failed` (um bloco por job).

### `job helmfile`

Resumo de um job de `helmfile apply|diff|sync`: o que foi comparado, o que mudou, o que o
helm atualizou e como terminou.

```bash
glab-debug -f compact job helmfile -p cix/devops/ci-deployments/cix-app-helmfiles 366779
```

```
job 366779 deploy/deploy:govone-v2-africa-prd status=success result=succeeded
url https://gitlab.govone.digital/cix/devops/ci-deployments/cix-app-helmfiles/-/jobs/366779
compared(10) admin-govone-africa central-atendimento-govone-africa … websocket-painel-africa
changed master/portal-govone-africa-portal Deployment
updated portal-govone-africa ns=master chart=govone-v2 version=0.1.7 duration=1s
```

`nochanges` aparece quando nada mudou (apply no-op). O corpo do diff só sai com `--diff`.
Saída `1` quando o job ou o helmfile falhou. Formato de referência: helmfile v0.171 +
helm-diff.

### Redação

Tudo que o `job log` e o `job helmfile` imprimem passa por `redact.py`, e **não há flag
para desligar**: o trace de um deploy roda sobre secrets decifrados e o `helm diff` mostra
o `env` dos Deployments em claro. Cobre `env` do Kubernetes com nome sensível (inclusive as
duas linhas `-`/`+` de um valor alterado), atribuições `NOME=valor`/`nome: valor`, senha em
URL, `Authorization`, chave AWS, JWT e token do GitLab. A saída traz `redacted=N` quando
houve corte. Quem precisa do valor cru abre o job na interface do GitLab.

## Formatos e custo em tokens

Quando a saída vai para um LLM (Claude Code lendo o resultado), o formato é custo direto.
Medição real do pipeline 58185, 7 jobs, `cl100k_base` como aproximação:

| formato | tokens | vs. `table` | quando usar |
|:---|---:|---:|:---|
| `compact --top 3` | 272 | **3,8×** | agente iterando sobre vários pipelines |
| `compact` | 325 | **3,2×** | default para consumo por LLM ou script |
| `table --no-timeline --no-stages` | 770 | 1,3× | terminal, quando só interessa o ranking de jobs |
| `table` | 1038 | 1,0× | leitura humana no terminal |
| `markdown` | 1211 | 0,9× | colar num doc do vault |
| `json` | 1976 | 0,5× | encadear com `jq` |

O `table` gasta **154 tokens só em espaços de alinhamento** e 64 nas linhas `─────`: ~21%
do output é decoração de terminal. O `json` é o mais caro porque repete o nome de cada
campo em cada job. O `compact` corta os dois: sem alinhamento, sem separadores, um
cabeçalho único em vez de chaves repetidas, e sem as seções deriváveis (stages e linha do
tempo saem das próprias linhas de job).

```
pipeline 58185 iid=511 path/to/project ref=homolog sha=1c2b2141 status=manual source=push
timing wall=1072.3s api=916.0s jobs_sum=916.8s busy=916.8s idle=155.6s(15%) queued=54.6s par=1.00x end=inferido
jobs stage/name status dur queued pct runner
build/build:arm64 success 665.3 1.0 62.0% 3036
sast-scan/sonarqube failed 189.6 1.0 17.7% 3036
deploy/update-tag success 26.3 2.2 2.5% 3096
erro sast-scan/trivy falhou (script_failure) — job 325030
```

Nenhum número foi perdido em relação ao `table` — o que sai são as seções redundantes e a
formatação. Alertas de nível `info` ficam de fora; `erro` e `aviso` permanecem.

Para tornar `compact` o default sem digitar a flag:

```toml
# ~/.config/glab-debug/config.toml
output_format = "compact"
```

## Configuração

Precedência: **flags de CLI → variáveis de ambiente → `~/.config/glab-debug/config.toml` → defaults**.

```toml
# ~/.config/glab-debug/config.toml
host = "gitlab.domain.local"
project = "cix/apps/admin-api"   # projeto padrão, opcional
output_format = "table"
timeout = 60
```

Variáveis de ambiente usam o prefixo `GLAB_DEBUG_` (`GLAB_DEBUG_HOST`,
`GLAB_DEBUG_PROJECT`, `GLAB_DEBUG_OUTPUT_FORMAT`, `GLAB_DEBUG_DEBUG=1`…). O `host` também
aceita `GITLAB_HOST`, que é a variável que o próprio `glab` já usa.

## Estrutura

```
src/glab_debug/
  cli.py           argparse: grupo → subcomando, e as flags globais
  config.py        Settings (pydantic-settings): env + TOML + defaults
  models.py        modelos pydantic — payloads da API e o relatório derivado
  gitlab.py        cliente: subprocess sobre `glab api`, paginação, normalização de projeto
  analysis.py      onde os números são derivados (união de intervalos, stages, alertas)
  fmt.py           cor, duração, tabela (texto e markdown), barra de tempo
  trace.py         limpeza do trace cru (prefixo do runner, ANSI, seções) e recortes
  redact.py        redação de credenciais, aplicada a todo texto de trace
  helmfile.py      parse do trace de helmfile apply/diff/sync
  commands/
    pipeline.py    grupo `pipeline`: `monitor` e `status`
    job.py         grupo `job`: `log` e `helmfile`
```

Para adicionar um grupo: crie `commands/<nome>.py` com uma função `register(subparsers)` e
liste o módulo em `commands/__init__.py::GROUPS`. A análise fica em `analysis.py` e a
apresentação em `fmt.py`, de modo que um subcomando novo herda formatação e config.
