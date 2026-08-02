"""Grupo `pipeline` — análise de execuções de CI.

Hoje só tem `monitor`: quebra um pipeline por job, dizendo quanto o pipeline
inteiro levou e quanto cada job consumiu dentro dele.
"""

from __future__ import annotations

import argparse

from .. import fmt
from ..analysis import analyse_pipeline
from ..config import Settings
from ..gitlab import GitLabClient, GlabError, infer_project_from_git
from ..models import JobReport, PipelineReport

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


# --------------------------------------------------------------------------- #
# Execução
# --------------------------------------------------------------------------- #


def run_monitor(args: argparse.Namespace, settings: Settings) -> int:
    project = args.project or settings.project or infer_project_from_git()
    if not project:
        raise GlabError(
            "Projeto não informado. Use --project, defina `project` em "
            "~/.config/glab-debug/config.toml ou rode dentro do repositório."
        )

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
