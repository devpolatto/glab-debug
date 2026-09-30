"""Grupo `pipeline` — análise de execuções de CI.

- `monitor`: quebra um pipeline por job, dizendo quanto o pipeline inteiro levou e
  quanto cada job consumiu dentro dele.
- `status`: em que pé está a pipeline (resolvida por ID, commit, ref ou MR) e quais
  jobs importam, opcionalmente esperando ela terminar.
"""

from __future__ import annotations

import argparse
import sys
import time

from .. import fmt
from ..analysis import analyse_pipeline, analyse_status, still_running
from ..config import Settings
from ..gitlab import GitLabClient, GlabError, infer_project_from_git
from ..models import Alert, JobReport, MergeRequest, Pipeline, PipelineReport, StatusReport

SORT_KEYS = ("duration", "start", "stage", "name")


def register(subparsers: argparse._SubParsersAction) -> None:
    group = subparsers.add_parser(
        "pipeline",
        help="Análise de pipelines de CI",
        description="Comandos de diagnóstico sobre pipelines do GitLab CI.",
    )
    commands = group.add_subparsers(dest="command", metavar="<subcomando>", required=True)

    monitor = commands.add_parser(
        "monitor",
        help="Quebra o tempo de um pipeline job a job",
        description=(
            "Mostra quanto o pipeline levou para terminar e quanto cada job demorou, "
            "com linha do tempo, agregação por stage e alertas."
        ),
    )
    monitor.add_argument("pipeline_id", type=int, metavar="<pipeline-id>", help="ID do pipeline.")
    monitor.add_argument(
        "-p",
        "--project",
        help="Projeto: path com namespace, ID numérico ou URL. Default: remote git ou config.",
    )
    monitor.add_argument(
        "--retried",
        action="store_true",
        help="Inclui tentativas anteriores de jobs reexecutados.",
    )
    monitor.add_argument(
        "--sort",
        choices=SORT_KEYS,
        default="duration",
        help="Ordenação da tabela de jobs (default: duration).",
    )
    monitor.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="Mostra apenas os N jobs mais longos; o resto vira uma linha de resumo.",
    )
    monitor.add_argument(
        "--no-timeline", action="store_true", help="Omite a linha do tempo."
    )
    monitor.add_argument(
        "--no-stages", action="store_true", help="Omite a agregação por stage."
    )
    monitor.set_defaults(handler=run_monitor)

    status = commands.add_parser(
        "status",
        help="Status de um pipeline e dos jobs que importam",
        description=(
            "Resolve o pipeline por ID, commit, ref ou MR e mostra o status dos jobs "
            "relevantes. Jobs manual/created/skipped viram contagem. Com --wait, espera "
            "o pipeline (ou os jobs filtrados) terminar."
        ),
    )
    target = status.add_mutually_exclusive_group(required=True)
    target.add_argument("pipeline_id", type=int, nargs="?", metavar="<pipeline-id>", help="ID do pipeline.")
    target.add_argument("--sha", help="Commit: usa o pipeline mais recente desse SHA.")
    target.add_argument("--ref", help="Branch ou tag: usa o último pipeline do ref.")
    target.add_argument(
        "--mr",
        type=int,
        metavar="IID",
        help="MR: se mergeado, o pipeline do merge commit no branch alvo; se aberto, o do MR.",
    )
    status.add_argument(
        "-p",
        "--project",
        help="Projeto: path com namespace, ID numérico ou URL. Default: remote git ou config.",
    )
    status.add_argument(
        "--job",
        dest="jobs",
        action="append",
        default=[],
        metavar="GLOB",
        help="Filtra jobs por nome (fnmatch, repetível). Jobs filtrados aparecem em qualquer estado.",
    )
    status.add_argument("--all", dest="show_all", action="store_true", help="Lista todos os jobs.")
    status.add_argument("--wait", action="store_true", help="Espera o pipeline (ou os jobs filtrados) terminar.")
    status.add_argument("--interval", type=int, default=15, metavar="SEG", help="Intervalo do --wait (default: 15).")
    status.add_argument(
        "--wait-timeout", type=int, default=1800, metavar="SEG", help="Teto do --wait (default: 1800)."
    )
    status.set_defaults(handler=run_status)


# --------------------------------------------------------------------------- #
# Execução
# --------------------------------------------------------------------------- #


def resolve_project(args: argparse.Namespace, settings: Settings) -> str:
    project = args.project or settings.project or infer_project_from_git()
    if not project:
        raise GlabError(
            "Projeto não informado. Use --project, defina `project` em "
            "~/.config/glab-debug/config.toml ou rode dentro do repositório."
        )
    return project


def run_monitor(args: argparse.Namespace, settings: Settings) -> int:
    project = resolve_project(args, settings)
    client = GitLabClient(settings)
    report = analyse_pipeline(
        project=client.project(project),
        pipeline=client.pipeline(project, args.pipeline_id),
        jobs=client.pipeline_jobs(project, args.pipeline_id, include_retried=args.retried),
        include_retried=args.retried,
    )

    renderers = {
        "json": lambda: report.model_dump_json(),
        "compact": lambda: render_compact(report, args),
        "markdown": lambda: render_markdown(report, args),
        "table": lambda: render_table(report, args),
    }
    print(renderers[settings.output_format]())

    return 1 if any(a.level == "erro" for a in report.alerts) else 0


def resolve_pipeline(
    client: GitLabClient, project: str, args: argparse.Namespace
) -> tuple[Pipeline, str | None, MergeRequest | None]:
    """Devolve o pipeline alvo, como ele foi achado (`via`) e o MR, quando for o caso."""
    if args.pipeline_id is not None:
        return client.pipeline(project, args.pipeline_id), None, None
    if args.ref:
        return client.latest_pipeline(project, args.ref), None, None
    if args.sha:
        found = client.pipelines_by_sha(project, args.sha)
        if not found:
            raise GlabError(f"Nenhum pipeline para o commit {args.sha}.")
        return client.pipeline(project, found[0].id), None, None

    mr = client.merge_request(project, args.mr)
    if mr.state == "merged":
        # Merge por fast-forward não gera merge commit: o head do MR vira o commit do alvo.
        sha = mr.merge_commit_sha or mr.squash_commit_sha or mr.sha
        if not sha:
            raise GlabError(f"MR !{mr.iid} mergeado, mas sem SHA de merge na API.")
        found = client.pipelines_by_sha(project, sha, ref=mr.target_branch)
        if not found:
            raise GlabError(
                f"MR !{mr.iid} mergeado em {mr.target_branch}, mas nenhum pipeline para {sha[:8]} "
                "(o CI pode não ter disparado para esse caminho)."
            )
        return client.pipeline(project, found[0].id), "merge_commit", mr
    if mr.head_pipeline is None:
        raise GlabError(f"MR !{mr.iid} ({mr.state}) não tem pipeline.")
    return client.pipeline(project, mr.head_pipeline.id), "head_pipeline", mr


def run_status(args: argparse.Namespace, settings: Settings) -> int:
    project = resolve_project(args, settings)
    client = GitLabClient(settings)
    proj = client.project(project)
    pipeline, via, mr = resolve_pipeline(client, project, args)
    jobs = client.pipeline_jobs(project, pipeline.id)

    waited: float | None = None
    timed_out = False
    if args.wait:
        started = time.monotonic()
        while still_running(pipeline, jobs, args.jobs):
            elapsed = time.monotonic() - started
            if elapsed >= args.wait_timeout:
                timed_out = True
                break
            if settings.output_format == "table":
                print(
                    f"[glab-debug] pipeline {pipeline.id} em {pipeline.status}, "
                    f"aguardando ({elapsed:.0f}s)…",
                    file=sys.stderr,
                )
            time.sleep(max(min(args.interval, args.wait_timeout - elapsed), 1))
            pipeline = client.pipeline(project, pipeline.id)
            jobs = client.pipeline_jobs(project, pipeline.id)
        waited = time.monotonic() - started

    report = analyse_status(
        project=proj,
        pipeline=pipeline,
        jobs=jobs,
        globs=args.jobs,
        show_all=args.show_all,
        via=via,
        merge_request=mr,
    )
    report.waited = waited
    report.timed_out = timed_out
    if timed_out:
        report.alerts.append(
            Alert(
                level="aviso",
                message=f"--wait-timeout de {args.wait_timeout}s estourado com o pipeline em {pipeline.status}.",
            )
        )

    renderers = {
        "json": lambda: report.model_dump_json(),
        "compact": lambda: render_status_compact(report),
        "markdown": lambda: render_status_markdown(report),
        "table": lambda: render_status_table(report),
    }
    print(renderers[settings.output_format]())
    return status_exit_code(report)


def status_exit_code(report: StatusReport) -> int:
    """`1` job falhou (prevalece) · `3` --wait estourou · `0` o resto, inclusive `manual`."""
    if any(a.level == "erro" for a in report.alerts):
        return 1
    if report.timed_out:
        return 3
    return 0


# --------------------------------------------------------------------------- #
# Ordenação e rótulos
# --------------------------------------------------------------------------- #


def _sorted_jobs(report: PipelineReport, key: str) -> list[JobReport]:
    jobs = list(report.jobs)
    if key == "duration":
        return sorted(jobs, key=lambda j: (j.duration is None, -(j.duration or 0.0), j.name))
    if key == "start":
        return sorted(jobs, key=lambda j: (j.offset is None, j.offset or 0.0, j.id))
    if key == "stage":
        return sorted(jobs, key=lambda j: (j.offset is None, j.stage, -(j.duration or 0.0)))
    return sorted(jobs, key=lambda j: j.name)


def _visible_jobs(
    report: PipelineReport, args: argparse.Namespace
) -> tuple[list[JobReport], int, float]:
    """Aplica `--top`, devolvendo também quantos jobs ficaram de fora e quanto somam."""
    jobs = _sorted_jobs(report, args.sort)
    top = getattr(args, "top", None)
    if not top or top >= len(jobs):
        return jobs, 0, 0.0
    hidden = jobs[top:]
    return jobs[:top], len(hidden), sum(j.duration or 0.0 for j in hidden)


def _attempt_label(job: JobReport) -> str:
    return f"{job.attempt}/{job.attempts_total}" if job.is_retry else "—"


def _runner_map(report: PipelineReport) -> dict[int, str]:
    return {
        j.runner_id: (j.runner_description or "")
        for j in report.jobs
        if j.runner_id is not None
    }


# --------------------------------------------------------------------------- #
# Terminal
# --------------------------------------------------------------------------- #


def render_table(report: PipelineReport, args: argparse.Namespace) -> str:
    p, t = report.pipeline, report.timing
    out: list[str] = []

    title = f"Pipeline #{p.iid or p.id} ({p.id})"
    out.append(f"{fmt.paint(title, 'bold')} · {report.project.path_with_namespace}")
    meta = [
        f"ref {fmt.paint(p.ref or '—', 'cyan')}",
        f"sha {(p.sha or '')[:8] or '—'}",
        f"source {p.source or '—'}",
        f"status {fmt.paint_status(p.status)}",
    ]
    if p.user and p.user.name:
        meta.append(f"por {p.user.name}")
    out.append("  " + " · ".join(meta))
    if report.commit and report.commit.title:
        out.append("  " + fmt.paint(fmt.truncate(report.commit.title, fmt.term_width() - 4), "dim"))
    if p.web_url:
        out.append("  " + fmt.paint(p.web_url, "dim"))

    out.append("")
    out.append(fmt.paint("Tempos", "bold"))
    for label, value in _timing_rows(report):
        out.append(f"  {label:<22}{value}")

    out.append("")
    out.append(fmt.paint("Jobs", "bold"))
    jobs, hidden, hidden_sum = _visible_jobs(report, args)
    out.append(_jobs_table(report, jobs).render())
    if hidden:
        out.append(fmt.paint(f"  … +{hidden} jobs omitidos ({fmt.duration(hidden_sum)})", "dim"))

    if not args.no_stages and len(report.stages) > 1:
        out.append("")
        out.append(fmt.paint("Stages", "bold"))
        out.append(_stages_table(report).render())

    runners = _runner_map(report)
    if runners:
        out.append("")
        out.append(fmt.paint("Runners", "bold"))
        for rid, desc in sorted(runners.items()):
            out.append(f"  {rid:<8}{fmt.truncate(desc or '—', fmt.term_width() - 12)}")

    if not args.no_timeline:
        block = _timeline(report)
        if block:
            out.append("")
            out.extend(block)

    if report.alerts:
        out.append("")
        out.append(fmt.paint("Alertas", "bold"))
        for alert in report.alerts:
            out.append(f"  {fmt.level_mark(alert.level)} {alert.message}")

    return "\n".join(out)


def _timing_rows(report: PipelineReport) -> list[tuple[str, str]]:
    t = report.timing
    finished = fmt.clock(t.finished_at)
    if t.finished_estimated:
        finished += fmt.paint("  (inferido)", "dim")

    rows = [
        ("criado", fmt.clock(t.created_at)),
        ("iniciado", fmt.clock(t.started_at)),
        ("finalizado", finished),
        (
            "relógio de parede",
            f"{fmt.paint(fmt.duration(t.wall), 'bold')}  ({fmt.seconds(t.wall)}s)",
        ),
    ]
    if t.api_duration is not None:
        rows.append(("execução (API)", f"{fmt.duration(t.api_duration)}  ({fmt.seconds(t.api_duration)}s)"))
    rows.append(("soma dos jobs", f"{fmt.duration(t.jobs_sum)}  ({fmt.seconds(t.jobs_sum)}s)"))
    rows.append(("tempo em fila", f"{fmt.duration(t.jobs_queued_sum)}  (jobs) / {fmt.duration(t.pipeline_queued)} (pipeline)"))
    idle_pct = f" ({t.idle / t.wall * 100:.0f}% ocioso)" if t.wall > 0 else ""
    rows.append(("executando / ocioso", f"{fmt.duration(t.busy)} / {fmt.duration(t.idle)}{idle_pct}"))
    rows.append(("paralelismo", f"{t.parallelism:.2f}×"))
    return rows


def _jobs_table(report: PipelineReport, jobs: list[JobReport]) -> fmt.Table:
    show_attempts = any(j.is_retry for j in report.jobs)
    headers = ["stage", "job", "status", "fila", "duração", "seg", "%"]
    aligns = "llrrrrr"
    if show_attempts:
        headers.append("tent.")
        aligns += "r"
    headers.append("runner")
    aligns += "r"

    table = fmt.Table(headers, aligns)
    for job in jobs:
        cells = [
            job.stage,
            job.name,
            fmt.paint_status(job.status),
            fmt.duration(job.queued),
            fmt.paint(fmt.duration(job.duration), "bold") if job.duration else fmt.duration(None),
            fmt.seconds(job.duration),
            fmt.percent(job.share),
        ]
        if show_attempts:
            cells.append(_attempt_label(job))
        cells.append(str(job.runner_id) if job.runner_id else "—")
        table.add(*cells)
    return table


def _stages_table(report: PipelineReport) -> fmt.Table:
    table = fmt.Table(["stage", "jobs", "soma", "span", "%"], "lrrrr")
    for stage in report.stages:
        table.add(
            stage.name,
            str(stage.jobs),
            fmt.duration(stage.duration_sum),
            fmt.duration(stage.span),
            fmt.percent(stage.share),
        )
    return table


def _timeline(report: PipelineReport, width: int | None = None) -> list[str]:
    jobs = [j for j in report.jobs if j.offset is not None and j.duration]
    if not jobs:
        return []

    total = report.timing.wall
    label_width = min(max(len(f"{j.stage}/{j.name}") for j in jobs), 30)
    bar_width = max((width or fmt.term_width()) - label_width - 16, 20)
    scale = total / bar_width if bar_width else 0

    lines = [
        fmt.paint("Linha do tempo", "bold")
        + fmt.paint(f"  (cada █ ≈ {scale:.1f}s · total {fmt.duration(total)})", "dim")
    ]
    for job in sorted(jobs, key=lambda j: (j.offset or 0.0, j.id)):
        bar, _ = fmt.timeline_bar(job.offset or 0.0, job.duration or 0.0, total, bar_width)
        label = fmt.truncate(f"{job.stage}/{job.name}", label_width).ljust(label_width)
        style = fmt.status_style(job.status)
        lines.append(f"  {label} {fmt.paint(bar, style) if style else bar} {fmt.duration(job.duration)}")
    return lines


# --------------------------------------------------------------------------- #
# Compact — denso, sem decoração, feito para ser lido por LLM ou script
# --------------------------------------------------------------------------- #


def render_compact(report: PipelineReport, args: argparse.Namespace) -> str:
    """Saída de linha única por registro, ~5× mais barata em tokens que a tabela.

    Sem alinhamento, sem separadores, sem stages nem linha do tempo (ambos deriváveis
    das linhas de job). Alertas ficam restritos a `erro` e `aviso`.
    """
    p, t = report.pipeline, report.timing

    head = [
        f"pipeline {p.id}",
        f"iid={p.iid}" if p.iid else "",
        report.project.path_with_namespace,
        f"ref={p.ref}" if p.ref else "",
        f"sha={(p.sha or '')[:8]}" if p.sha else "",
        f"status={p.status}",
        f"source={p.source}" if p.source else "",
    ]

    idle_pct = f"({t.idle / t.wall * 100:.0f}%)" if t.wall > 0 else ""
    timing = [
        "timing",
        f"wall={t.wall:.1f}s",
        f"api={t.api_duration:.1f}s" if t.api_duration is not None else "",
        f"jobs_sum={t.jobs_sum:.1f}s",
        f"busy={t.busy:.1f}s",
        f"idle={t.idle:.1f}s{idle_pct}",
        f"queued={t.jobs_queued_sum:.1f}s",
        f"par={t.parallelism:.2f}x",
        "end=inferido" if t.finished_estimated else "",
    ]

    lines = [" ".join(x for x in head if x), " ".join(x for x in timing if x)]

    show_attempts = any(j.is_retry for j in report.jobs)
    header = "jobs stage/name status dur queued pct runner"
    lines.append(header + " attempt" if show_attempts else header)

    visible, hidden, hidden_sum = _visible_jobs(report, args)
    for job in visible:
        row = [
            f"{job.stage}/{job.name}",
            job.status,
            fmt.seconds(job.duration, "-"),
            fmt.seconds(job.queued, "-"),
            f"{job.share * 100:.1f}%" if job.share else "-",
            str(job.runner_id) if job.runner_id else "-",
        ]
        if show_attempts:
            row.append(f"{job.attempt}/{job.attempts_total}" if job.is_retry else "-")
        lines.append(" ".join(row))
    if hidden:
        lines.append(f"+{hidden} jobs omitidos soma={hidden_sum:.1f}s")

    relevant = [a for a in report.alerts if a.level in ("erro", "aviso")]
    lines += [f"{a.level} {a.message}" for a in relevant]

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def render_markdown(report: PipelineReport, args: argparse.Namespace) -> str:
    p, t = report.pipeline, report.timing
    out: list[str] = [f"# Pipeline #{p.iid or p.id} — {report.project.path_with_namespace}", ""]

    meta = [
        f"- **Pipeline**: [{p.id}]({p.web_url})" if p.web_url else f"- **Pipeline**: {p.id}",
        f"- **Ref**: `{p.ref or '—'}` @ `{(p.sha or '')[:8]}`",
        f"- **Status**: `{p.status}` · source `{p.source or '—'}`",
        f"- **Criado**: {fmt.clock(t.created_at)} · **Finalizado**: {fmt.clock(t.finished_at)}"
        + (" _(inferido)_" if t.finished_estimated else ""),
    ]
    if report.commit and report.commit.title:
        meta.append(f"- **Commit**: {report.commit.title}")
    out += meta + ["", "## Tempos", ""]

    totals = fmt.Table(["métrica", "valor", "segundos"], "lrr")
    totals.add("relógio de parede", fmt.duration(t.wall), fmt.seconds(t.wall))
    if t.api_duration is not None:
        totals.add("execução (API)", fmt.duration(t.api_duration), fmt.seconds(t.api_duration))
    totals.add("soma dos jobs", fmt.duration(t.jobs_sum), fmt.seconds(t.jobs_sum))
    totals.add("tempo em fila (jobs)", fmt.duration(t.jobs_queued_sum), fmt.seconds(t.jobs_queued_sum))
    totals.add("executando", fmt.duration(t.busy), fmt.seconds(t.busy))
    totals.add("ocioso", fmt.duration(t.idle), fmt.seconds(t.idle))
    totals.add("paralelismo", f"{t.parallelism:.2f}×", "—")
    out += [totals.render_markdown(), "", "## Jobs", ""]

    show_attempts = any(j.is_retry for j in report.jobs)
    headers = ["stage", "job", "status", "fila", "duração", "seg", "%"]
    aligns = "llrrrrr"
    if show_attempts:
        headers.append("tent.")
        aligns += "r"
    headers.append("runner")
    aligns += "r"

    visible, hidden, hidden_sum = _visible_jobs(report, args)
    jobs_table = fmt.Table(headers, aligns)
    for job in visible:
        name = f"[{job.name}]({job.web_url})" if job.web_url else job.name
        cells = [
            job.stage,
            name,
            f"`{job.status}`",
            fmt.duration(job.queued),
            fmt.duration(job.duration),
            fmt.seconds(job.duration),
            fmt.percent(job.share),
        ]
        if show_attempts:
            cells.append(_attempt_label(job))
        cells.append(str(job.runner_id) if job.runner_id else "—")
        jobs_table.add(*cells)
    out.append(jobs_table.render_markdown())
    if hidden:
        out += ["", f"_+{hidden} jobs omitidos ({fmt.duration(hidden_sum)})_"]

    if not args.no_stages and len(report.stages) > 1:
        out += ["", "## Stages", "", _stages_table(report).render_markdown()]

    runners = _runner_map(report)
    if runners:
        rt = fmt.Table(["runner", "descrição"], "rl")
        for rid, desc in sorted(runners.items()):
            rt.add(str(rid), desc or "—")
        out += ["", "## Runners", "", rt.render_markdown()]

    if not args.no_timeline:
        block = _timeline(report, width=92)
        if block:
            body = [fmt.strip_ansi(line) for line in block[1:]]
            out += ["", "## Linha do tempo", "", "```text", *body, "```"]

    if report.alerts:
        out += ["", "## Alertas", ""]
        marks = {"erro": "❌", "aviso": "⚠️", "info": "ℹ️"}
        out += [f"- {marks.get(a.level, '•')} {a.message}" for a in report.alerts]

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# pipeline status — renderizadores
# --------------------------------------------------------------------------- #


def _status_head(report: StatusReport) -> list[str]:
    p = report.pipeline
    mr = report.merge_request
    return [
        f"pipeline {p.id}",
        report.project.path_with_namespace,
        f"ref={p.ref}" if p.ref else "",
        f"sha={(p.sha or '')[:8]}" if p.sha else "",
        f"status={p.status}",
        f"source={p.source}" if p.source else "",
        f"via={report.via}" if report.via else "",
        f"mr=!{mr.iid}" if mr else "",
        f"mr_state={mr.state}" if mr else "",
        f"waited={report.waited:.0f}s" if report.waited is not None else "",
        "timeout" if report.timed_out else "",
    ]


def render_status_compact(report: StatusReport) -> str:
    counts = " ".join(f"{k}={v}" for k, v in report.counts.items())
    lines = [
        " ".join(x for x in _status_head(report) if x),
        f"url {report.pipeline.web_url}" if report.pipeline.web_url else "",
        f"counts {counts}" if counts else "counts -",
        f"filter {' '.join(report.filters)}" if report.filters else "",
        "jobs stage/name status dur reason id",
    ]
    for job in report.jobs:
        lines.append(
            " ".join(
                [
                    f"{job.stage}/{job.name}",
                    job.status_label,
                    fmt.seconds(job.duration, "-"),
                    job.failure_reason or "-",
                    str(job.id),
                ]
            )
        )
    if report.hidden:
        lines.append(f"+{report.hidden} jobs fora da lista")
    lines += [f"{a.level} {a.message}" for a in report.alerts]
    return "\n".join(x for x in lines if x)


def _status_jobs_table(report: StatusReport) -> fmt.Table:
    table = fmt.Table(["stage", "job", "status", "duração", "motivo", "id"], "lllrlr")
    for job in report.jobs:
        table.add(
            job.stage,
            job.name,
            fmt.paint_status(job.status) + (" (allowed)" if job.allow_failure and job.status == "failed" else ""),
            fmt.duration(job.duration),
            job.failure_reason or "—",
            str(job.id),
        )
    return table


def render_status_table(report: StatusReport) -> str:
    p = report.pipeline
    out = [f"{fmt.paint(f'Pipeline {p.id}', 'bold')} · {report.project.path_with_namespace}"]
    meta = [
        f"ref {fmt.paint(p.ref or '—', 'cyan')}",
        f"sha {(p.sha or '')[:8] or '—'}",
        f"status {fmt.paint_status(p.status)}",
        f"source {p.source or '—'}",
    ]
    out.append("  " + " · ".join(meta))
    if report.merge_request:
        mr = report.merge_request
        via = {"merge_commit": "pipeline do merge commit", "head_pipeline": "pipeline do próprio MR"}
        out.append(f"  MR !{mr.iid} ({mr.state}) → {via.get(report.via or '', report.via or '—')}")
    if report.waited is not None:
        out.append(f"  aguardou {fmt.duration(report.waited)}" + (" (timeout)" if report.timed_out else ""))
    if p.web_url:
        out.append("  " + fmt.paint(p.web_url, "dim"))

    out += ["", fmt.paint("Contagem", "bold")]
    out.append("  " + (" · ".join(f"{k} {v}" for k, v in report.counts.items()) or "—"))
    if report.filters:
        out.append("  filtro: " + ", ".join(report.filters))

    out += ["", fmt.paint("Jobs", "bold"), _status_jobs_table(report).render()]
    if report.hidden:
        out.append(fmt.paint(f"  … +{report.hidden} jobs fora da lista (use --all)", "dim"))

    if report.alerts:
        out += ["", fmt.paint("Alertas", "bold")]
        out += [f"  {fmt.level_mark(a.level)} {a.message}" for a in report.alerts]
    return "\n".join(out)


def render_status_markdown(report: StatusReport) -> str:
    p = report.pipeline
    pid = f"[{p.id}]({p.web_url})" if p.web_url else str(p.id)
    out = [f"# Pipeline {p.id} — {report.project.path_with_namespace}", ""]
    out.append(f"- **Pipeline**: {pid} · status `{p.status}` · source `{p.source or '—'}`")
    out.append(f"- **Ref**: `{p.ref or '—'}` @ `{(p.sha or '')[:8]}`")
    if report.merge_request:
        mr = report.merge_request
        out.append(f"- **MR**: !{mr.iid} (`{mr.state}`) via `{report.via}`")
    if report.waited is not None:
        out.append(f"- **Aguardou**: {fmt.duration(report.waited)}" + (" _(timeout)_" if report.timed_out else ""))
    out.append("- **Contagem**: " + (", ".join(f"{k} {v}" for k, v in report.counts.items()) or "—"))
    if report.filters:
        out.append("- **Filtro**: " + ", ".join(f"`{g}`" for g in report.filters))

    table = fmt.Table(["stage", "job", "status", "duração", "motivo", "id"], "lllrlr")
    for job in report.jobs:
        name = f"[{job.name}]({job.web_url})" if job.web_url else job.name
        table.add(job.stage, name, f"`{job.status_label}`", fmt.duration(job.duration), job.failure_reason or "—", str(job.id))
    out += ["", "## Jobs", "", table.render_markdown()]
    if report.hidden:
        out += ["", f"_+{report.hidden} jobs fora da lista_"]

    if report.alerts:
        marks = {"erro": "❌", "aviso": "⚠️", "info": "ℹ️"}
        out += ["", "## Alertas", ""] + [f"- {marks.get(a.level, '•')} {a.message}" for a in report.alerts]
    return "\n".join(out)
