# Spec: `pipeline status`, `job log`, `job helmfile` + agent `gitlab-pipeline`

Status: **proposta**, aguardando revisão. Data: 2026-09-30.

## Motivação

Hoje o `glab-debug` só mede tempo (`pipeline monitor`). Para responder "o deploy do MR
passou? o que mudou?", a sessão monta a mesma sequência ad-hoc de `glab api`: achar a
pipeline pelo merge commit, listar jobs, baixar o trace inteiro e fazer grep de
`has changed|UPDATED RELEASES`. Essa sequência se repetiu nos MRs !49 e !50 do
`cix-app-helmfiles` (29 e 30/09). Ela tem três custos:

1. **Contexto:** o trace inteiro entra na sessão para extrair meia dúzia de linhas.
2. **Ruído:** em pipelines GitOps, a maioria dos jobs fica em `manual`/`created`. Na
   pipeline 67511, só 1 de 16 linhas do `monitor` era relevante.
3. **Segurança:** o trace do `helmfile` roda sobre secrets decifrados (SOPS), e o
   `helm diff` imprime em claro o `env` dos Deployments. Um grep mal escrito vaza
   credencial para o transcript.

## Escopo

| Comando | Responde |
|---|---|
| `pipeline status` | "a pipeline X / do MR !N / do commit S passou? em que pé está?" |
| `job log` | "por que o job falhou?" (trecho do log, limpo e redigido) |
| `job helmfile` | "o que o `helmfile apply` / `diff` mudou?" |

Fora do escopo: disparar ou reexecutar jobs, `play` de job manual e qualquer escrita. A
ferramenta continua **somente leitura**, só com `GET`.

## 1. `pipeline status`

```
glab-debug -f compact pipeline status [-p PROJECT]
    (<pipeline-id> | --sha SHA | --mr IID | --ref REF)
    [--job GLOB]... [--all]
    [--wait] [--interval 15] [--wait-timeout 1800]
```

**Resolução do alvo:**

| Entrada | Rota | Observação |
|---|---|---|
| `<pipeline-id>` | `pipelines/:id` | direto |
| `--sha` | `pipelines?sha=` | a mais recente, quando houver mais de uma |
| `--ref` | `pipelines/latest?ref=` | a última do branch/tag |
| `--mr` | `merge_requests/:iid` | **MR mergeado** → pipeline do `merge_commit_sha` (ou `squash_commit_sha`) no branch alvo, que é a do deploy. **MR aberto** → `head_pipeline`. A saída diz qual das duas foi usada (`via=merge_commit` / `via=head_pipeline`) |

**Saída `compact`** (exemplo com a 67511):

```
pipeline 67511 cix/devops/ci-deployments/cix-app-helmfiles ref=main sha=6f7a024d status=manual source=push via=merge_commit mr=!50
counts success=1 manual=8 created=6 skipped=1
jobs stage/name status dur reason id
deploy/deploy:govone-v2-africa-prd success 86.8 - 366779
```

- Por padrão, só entram na lista os jobs **informativos**: `failed`, `running`,
  `pending`, `preparing`, `waiting_for_resource`, `canceled` e `success`. Os estados
  `manual`, `created` e `skipped` viram só contagem na linha `counts`. `--all` lista
  todos os jobs.
- `--job GLOB` (repetível, `fnmatch`) filtra por nome, ex.: `--job 'deploy:*africa-prd'`.
  Com filtro, os jobs que batem aparecem **em qualquer estado**: para quem pergunta de
  um job específico, `manual` é informação.
- `reason` = `failure_reason` da API; `allow_failure` aparece como `failed(allowed)`.
- Não recalcula tempo: para gargalo, o comando é o `monitor`.

**`--wait`:** faz polling de `pipelines/:id` a cada `--interval` segundos até o status
sair de `created|waiting_for_resource|preparing|pending|running`. **`manual` conta como
estado final**, porque é onde as pipelines GitOps param. Com `--job`, espera só os jobs
filtrados saírem desses estados. Sem saída intermediária no `compact`, só o resultado
final.

**Códigos de saída:** `0` ok (inclui `manual`) · `1` algum job relevante `failed` sem
`allow_failure` · `2` erro de execução · `3` `--wait-timeout` estourado com a pipeline
ainda em andamento · `130` interrupção.

## 2. `job log`

```
glab-debug -f compact job log [-p PROJECT]
    (<job-id> | --pipeline ID --job NAME | --pipeline ID --failed)
    [--tail 80] [--grep REGEX] [--context 3] [--sections]
```

- **Limpeza do trace:** remove o prefixo `<timestamp> <stream><O|E>`, junta as
  continuações (`+`), tira ANSI e `\r` e descarta os marcadores
  `section_start`/`section_end`.
- `--tail N` (default 80) pega as últimas N linhas. `--grep` devolve os matches com
  `--context` linhas em volta, e as janelas sobrepostas se fundem.
- `--failed` com `--pipeline` aplica o comando a cada job `failed` da pipeline, com um
  cabeçalho por job. É o caso de uso do `triage`.
- `--sections` lista as seções do GitLab (`prepare_executor`, `step_script`…) com a
  duração de cada uma, sem o conteúdo.
- **Teto duro de 400 linhas** na saída, mesmo com `--tail` maior, e uma linha
  `truncado=N` quando cortar.
- A saída sempre passa pela **redação** (seção 4).

## 3. `job helmfile`

```
glab-debug -f compact job helmfile [-p PROJECT]
    (<job-id> | --pipeline ID --job NAME)
    [--diff]
```

Faz o parse do trace de `helmfile apply|diff|sync`:

```
job 366779 deploy:govone-v2-africa-prd status=success
compared admin-govone-africa central-atendimento-govone-africa ... traefik
changed master/portal-govone-africa-portal Deployment
updated portal-govone-africa ns=master chart=govone-v2 version=0.1.7
result success
```

- `compared`: releases da linha `Comparing release=...`.
- `changed`: cada `"<ns>, <nome>, <Kind> (<group>) has changed:"`, e também
  `has been added`/`has been removed`.
- `updated`: a tabela `UPDATED RELEASES:` (e `DELETED RELEASES:`).
- `result`: `Job succeeded` / `Job failed` / o erro do helmfile (`Error: ...`).
- `--diff` inclui o corpo do diff de cada objeto, **redigido**. Sem a flag, o diff nunca
  sai.
- Nada mudou → `changed` vazio + `nochanges`. É a resposta para "o apply foi no-op?".

## 4. Redação (`redact.py`, sempre ligada)

Aplicada a toda saída de `job log` e `job helmfile --diff`, **sem flag para desligar**.
Quem precisa do valor cru abre o job na UI.

| Padrão | Exemplo | Vira |
|---|---|---|
| env Kubernetes: `- name: <SENSÍVEL>` seguido de `value: X` | `name: DB_URL` / `value: postgres://…` | `value: <redacted>` |
| atribuição `<SENSÍVEL>=X` ou `<SENSÍVEL>: X` | `AWS_SECRET_ACCESS_KEY=…` | `AWS_SECRET_ACCESS_KEY=<redacted>` |
| userinfo em URL | `amqp://user:pw@host` | `amqp://user:<redacted>@host` |
| chave AWS | `AKIA[0-9A-Z]{16}` | `<redacted:aws-key>` |
| JWT | `eyJ….….…` | `<redacted:jwt>` |
| `Authorization: Bearer …`, `PRIVATE-TOKEN: …` | | `<redacted>` |

`<SENSÍVEL>` = nome que contém `PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|ACCESS_KEY|
PRIVATE_KEY|CREDENTIAL|DSN`, ou que termina em `_URL`/`_URI` com userinfo. Ao final vem a
linha `redacted=N`, para quem lê saber que houve corte.

Testes com fixtures sintéticas cobrem cada padrão, além de um **teste negativo**: texto
comum (`name: portal`, `image: ...:244-main-0541f926`) não pode ser redigido.

## 5. Arquitetura (segue as quatro camadas do CLAUDE.md)

| Camada | Mudança |
|---|---|
| `gitlab.py` | `get_text()` para o trace (o `get()` faz `json.loads`); `merge_request()`, `pipelines_by_sha()`, `latest_pipeline(ref)`, `job()` e `job_trace()` |
| `models.py` | `MergeRequest`; relatórios `StatusReport`, `LogReport` e `HelmfileReport` |
| `trace.py` (novo) | limpeza do trace e extração de seções, puro |
| `helmfile.py` (novo) | parse do trace do helmfile, puro |
| `redact.py` (novo) | redação, pura |
| `commands/pipeline.py` | subcomando `status` |
| `commands/job.py` (novo) | grupo `job` com `log` e `helmfile`, registrado em `GROUPS` |

Os quatro formatos em todo comando, com a invariante do `compact` estendida: não perde
nenhum dado em relação ao `table`. Nada de rede nos testes.

## 6. Agent `gitlab-pipeline` (`~/.claude/agents/gitlab-pipeline.md`)

- **Modelo:** `sonnet`. **Tools:** `Bash`, `Read`. O agent só executa `glab-debug` e
  `glab api` com GET. Isso vai escrito nas instruções, porque o frontmatter não restringe
  argumentos do Bash.
- **Entrada:** pergunta em linguagem natural ("o deploy do MR !50 no cix-app-helmfiles
  passou? o que mudou?", "por que o build 58087 do admin-api falhou?").
- **Fluxo:** resolve o alvo com `pipeline status`. Se algo falhou → `job log --failed`
  com `--tail`/`--grep`. Se é job de helmfile → `job helmfile`. Para tempo, usa o
  `monitor`.
- **Saída:** veredito em uma linha, depois a tabela curta dos jobs relevantes, o que
  mudou (helmfile) ou o trecho do erro (redigido), os links e as **Ressalvas** (ex.:
  "`via=head_pipeline`: o MR não está mergeado, então isto não é o deploy").
- **Regras duras:** nunca `glab api .../trace` direto (sempre `job log`, que redige),
  nunca `--diff` sem necessidade e nunca repetir valores que parecerem credencial.

## 7. Skill `glab-debug` (ampliar, não criar outra)

- **Descrição:** passa a cobrir status, log de falha e o que o deploy mudou, além de
  tempo.
- **Seção nova "Quando delegar ao agent"**:
  - status, log e helmfile → agent `gitlab-pipeline`, para manter o trace fora do contexto
  - uma consulta pontual de tempo (`monitor`) → direto, porque já é barata
- **Regra do `compact`**, os comandos novos e os códigos de saída `1`/`3`.
- **Aviso sobre o `cix-app-helmfiles`:** o merge em `clusters/**/` dispara o deploy,
  inclusive em PRD (`when: always`). O comentário "apenas manual" no `.gitlab-ci.yml`
  está desatualizado.
- **Correção do caminho do repo** e do `config.toml` (ambos tinham sumido desta
  máquina).

## 8. Ordem de entrega

1. `redact.py` + `trace.py` + testes: é a base de segurança, e vem antes de qualquer
   comando que leia trace.
2. `pipeline status` (sem `--wait`) + testes.
3. `job log` + testes.
4. `job helmfile` + testes, com fixture sintética derivada do formato real do job 366779.
5. `--wait`.
6. Agent + atualização da skill + validação ponta a ponta contra a pipeline 67511 e uma
   pipeline com falha real.

Cada passo vira um commit em `feat/status-log-helmfile` a partir da `development`, e um
PR para a `development` no final. O CI e a promoção para a `master` já existem.

## Em aberto

- **Nome do agent:** `gitlab-pipeline` ou `pipeline-inspector`?
- **`--wait` no CLI vs `Monitor` na sessão principal.** A proposta é o CLI, porque serve
  a agent, script e humano.
- **`job helmfile` tem escopo GitOps da Equanimus.** Se o repo for público/genérico, vale
  deixar claro no README que o parser é para o formato do helmfile ≥ 0.17x.
