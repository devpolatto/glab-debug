"""Grupo `job` — leitura do trace de jobs.

- `log`: trecho do log (fim, busca ou seções), limpo e redigido.
- `helmfile`: o que um job de `helmfile apply|diff|sync` comparou, mudou e atualizou.

Todo texto de trace que sai daqui passa pela redação de `redact.py`, sem exceção.
"""

from __future__ import annotations

import argparse
from pathlib import PurePosixPath

from .. import fmt, helmfile
from ..analysis import job_matches
from ..config import Settings
from ..gitlab import GitLabClient, GlabError
from ..models import HelmfileReport, Job, JobsOutput, LogReport, StatusJob
from ..redact import Redactor, redact
from ..trace import MAX_LINES, clean, gaps, grep, tail
from .pipeline import resolve_project


def _add_selection(parser: argparse.ArgumentParser, allow_failed: bool = True) -> None:
    parser.add_argument("job_id", type=int, nargs="?", metavar="<job-id>", help="ID do job.")
    parser.add_argument(
        "-p",
        "--project",
        help="Projeto: path com namespace, ID numérico ou URL. Default: remote git ou config.",
    )
    parser.add_argument("--pipeline", type=int, metavar="ID", help="Seleciona jobs de um pipeline.")
    parser.add_argument(
        "--job",
        dest="jobs",
        action="append",
        default=[],
        metavar="GLOB",
        help="Com --pipeline: jobs pelo nome (fnmatch, repetível).",
    )
    if allow_failed:
        parser.add_argument("--failed", action="store_true", help="Com --pipeline: todos os jobs que falharam.")


def register(subparsers: argparse._SubParsersAction) -> None:
    group = subparsers.add_parser(
        "job",
        help="Leitura do trace de jobs (log e helmfile)",
        description="Comandos sobre o trace de jobs do GitLab CI. Toda saída é redigida.",
    )
    commands = group.add_subparsers(dest="command", metavar="<subcomando>", required=True)

    log = commands.add_parser(
        "log",
        help="Trecho do log de um job, limpo e redigido",
        description=(
            "Mostra o fim do log (--tail), as linhas que batem com uma regex (--grep) ou só "
            f"as seções com duração (--sections). Teto de {MAX_LINES} linhas por job."
        ),
    )
    _add_selection(log)
    log.add_argument("--tail", type=int, default=80, metavar="N", help="Últimas N linhas (default: 80).")
    log.add_argument("--grep", metavar="REGEX", help="Linhas que batem com a regex (Python).")
    log.add_argument("--context", type=int, default=3, metavar="N", help="Contexto do --grep (default: 3).")
    log.add_argument("--sections", action="store_true", help="Só as seções do GitLab e a duração de cada uma.")
    log.set_defaults(handler=run_log)

    hf = commands.add_parser(
        "helmfile",
        help="Resumo de um job de helmfile apply/diff/sync",
        description=(
            "Releases comparados, objetos alterados, tabela UPDATED/DELETED/FAILED RELEASES e o "
            "resultado do job. O corpo do diff só sai com --diff, e sempre redigido."
        ),
    )
    _add_selection(hf, allow_failed=False)
    hf.add_argument("--diff", action="store_true", help="Inclui o corpo do diff de cada objeto (redigido).")
    hf.set_defaults(handler=run_helmfile)


# --------------------------------------------------------------------------- #
# Seleção
# --------------------------------------------------------------------------- #


def select_jobs(client: GitLabClient, project: str, args: argparse.Namespace) -> list[Job]:
    failed = getattr(args, "failed", False)
    if args.job_id is not None:
        if args.pipeline or args.jobs or failed:
            raise GlabError("Use <job-id> sozinho, ou --pipeline com --job/--failed.")
        return [client.job(project, args.job_id)]
    if args.pipeline is None or not (args.jobs or failed):
        raise GlabError("Informe <job-id>, ou --pipeline com --job GLOB ou --failed.")

    jobs = client.pipeline_jobs(project, args.pipeline)
    chosen = [
        j for j in jobs if (failed and j.status == "failed") or (args.jobs and job_matches(j.name, args.jobs))
    ]
    if not chosen:
        what = "falho" if failed and not args.jobs else f"com nome {', '.join(args.jobs)}"
        raise GlabError(f"Nenhum job {what} no pipeline {args.pipeline}.")
    return chosen


def _info(job: Job) -> StatusJob:
    return StatusJob(
        id=job.id,
        name=job.name,
        stage=job.stage,
        status=job.status,
        allow_failure=job.allow_failure,
        failure_reason=job.failure_reason,
        duration=job.duration,
        web_url=job.web_url,
    )


# --------------------------------------------------------------------------- #
# job log
# --------------------------------------------------------------------------- #


def build_log(job: Job, raw: str, args: argparse.Namespace) -> LogReport:
    trace = clean(raw)
    lines, redacted = redact(trace.lines)
    info = _info(job)
    if args.sections:
        return LogReport(job=info, mode="sections", total_lines=len(lines), sections=trace.sections, redacted=0)
    if args.grep:
        ex = grep(lines, args.grep, context=args.context)
        mode = "grep"
    else:
        ex = tail(lines, args.tail)
        mode = "tail"
    # Conta só o que foi cortado dentro do trecho exibido — é o que quem lê precisa saber.
    in_view = sum(1 for _, text in ex.rows if "<redacted" in text) if redacted else 0
    return LogReport(
        job=info,
        mode=mode,
        pattern=args.grep,
        total_lines=ex.total_lines,
        rows=ex.rows,
        truncated=ex.truncated,
        matches=ex.matches,
        redacted=in_view,
    )


def run_log(args: argparse.Namespace, settings: Settings) -> int:
    project = resolve_project(args, settings)
    client = GitLabClient(settings)
    reports = [build_log(job, client.job_trace(project, job.id), args) for job in select_jobs(client, project, args)]
    renderers = {
        "json": lambda: JobsOutput(project=client.project(project), reports=reports).model_dump_json(),
        "compact": lambda: "\n".join(render_log_compact(r) for r in reports),
        "markdown": lambda: "\n\n".join(render_log_markdown(r) for r in reports),
        "table": lambda: "\n\n".join(render_log_table(r) for r in reports),
    }
    print(renderers[settings.output_format]())
    return 0


def _log_head(r: LogReport) -> list[str]:
    j = r.job
    return [
        f"job {j.id}",
        f"{j.stage}/{j.name}",
        f"status={j.status_label}",
        f"reason={j.failure_reason}" if j.failure_reason else "",
        f"lines={r.total_lines}",
        f"tail={len(r.rows)}" if r.mode == "tail" else "",
        f"grep={r.pattern!r} matches={r.matches}" if r.mode == "grep" else "",
        f"truncated={r.truncated}" if r.truncated else "",
        f"redacted={r.redacted}" if r.redacted else "",
    ]


def _body(r: LogReport, number_width: int = 0) -> list[str]:
    if r.mode == "sections":
        return [
            f"section {s.name} {fmt.seconds(s.duration, '-')}s lines={s.start_line}-{s.end_line or '?'}"
            for s in r.sections
        ]
    out: list[str] = []
    for item in gaps(r.rows):
        if item is None:
            out.append("…")
        else:
            number, text = item
            out.append(f"{str(number).rjust(number_width)}| {text}")
    return out


def render_log_compact(r: LogReport) -> str:
    lines = [" ".join(x for x in _log_head(r) if x)]
    if r.job.web_url:
        lines.append(f"url {r.job.web_url}")
    return "\n".join(lines + _body(r))


def render_log_table(r: LogReport) -> str:
    j = r.job
    out = [f"{fmt.paint(f'Job {j.id}', 'bold')} · {j.stage}/{j.name} · {fmt.paint_status(j.status)}"]
    meta = [x for x in _log_head(r)[3:] if x]
    out.append("  " + " · ".join(meta))
    if j.web_url:
        out.append("  " + fmt.paint(j.web_url, "dim"))
    width = len(str(r.rows[-1][0])) if r.rows else 0
    out += [""] + [f"  {line}" for line in _body(r, width)]
    return "\n".join(out)


def render_log_markdown(r: LogReport) -> str:
    j = r.job
    title = f"[{j.name}]({j.web_url})" if j.web_url else j.name
    meta = " · ".join(x for x in _log_head(r)[2:] if x)
    return "\n".join([f"## Job {j.id} — {title}", "", f"`{meta}`", "", "```text", *_body(r), "```"])


# --------------------------------------------------------------------------- #
# job helmfile
# --------------------------------------------------------------------------- #


def build_helmfile(job: Job, raw: str, show_diff: bool) -> HelmfileReport:
    summary = helmfile.parse(clean(raw).lines)
    redactor = Redactor()
    # Os erros podem citar valores (ex.: um Secret mal formado): redige sempre.
    summary.errors = redactor.lines(summary.errors)
    if show_diff:
        for change in summary.changes:
            change.diff = Redactor().lines(change.diff) if change.diff else []
        redacted = redactor.count + sum(
            sum(1 for line in c.diff if "<redacted" in line) for c in summary.changes
        )
    else:
        for change in summary.changes:
            change.diff = []
        redacted = redactor.count
    return HelmfileReport(job=_info(job), summary=summary, show_diff=show_diff, redacted=redacted)


def run_helmfile(args: argparse.Namespace, settings: Settings) -> int:
    project = resolve_project(args, settings)
    client = GitLabClient(settings)
    reports = [
        build_helmfile(job, client.job_trace(project, job.id), args.diff)
        for job in select_jobs(client, project, args)
    ]
    renderers = {
        "json": lambda: JobsOutput(project=client.project(project), reports=reports).model_dump_json(),
        "compact": lambda: "\n".join(render_helmfile_compact(r) for r in reports),
        "markdown": lambda: "\n\n".join(render_helmfile_markdown(r) for r in reports),
        "table": lambda: "\n\n".join(render_helmfile_table(r) for r in reports),
    }
    print(renderers[settings.output_format]())
    failed = any(r.summary.result == "failed" or r.job.status == "failed" for r in reports)
    return 1 if failed else 0


def _chart_name(chart: str | None) -> str:
    return PurePosixPath(chart).name if chart else "-"


def render_helmfile_compact(r: HelmfileReport) -> str:
    j, s = r.job, r.summary
    head = [
        f"job {j.id}",
        f"{j.stage}/{j.name}",
        f"status={j.status_label}",
        f"result={s.result or '-'}",
        f"redacted={r.redacted}" if r.redacted else "",
    ]
    lines = [" ".join(x for x in head if x)]
    if j.web_url:
        lines.append(f"url {j.web_url}")
    lines.append(f"compared({len(s.compared)}) " + (" ".join(s.compared) or "-"))
    lines += [f"{c.action} {c.namespace}/{c.name} {c.kind}" for c in s.changes]
    lines += [
        f"{row.table.lower()} {row.name} ns={row.namespace or '-'} chart={_chart_name(row.chart)} "
        f"version={row.version or '-'}" + (f" duration={row.duration}" if row.duration else "")
        for row in s.releases
    ]
    if s.no_changes:
        lines.append("nochanges")
    lines += [f"error {e}" for e in s.errors]
    if r.show_diff:
        for c in s.changes:
            lines.append(f"diff {c.namespace}/{c.name} {c.kind}")
            lines += c.diff
    return "\n".join(lines)


def render_helmfile_table(r: HelmfileReport) -> str:
    j, s = r.job, r.summary
    out = [
        f"{fmt.paint(f'Job {j.id}', 'bold')} · {j.stage}/{j.name} · {fmt.paint_status(j.status)} "
        f"· resultado {fmt.paint_status('success' if s.result == 'succeeded' else (s.result or '—'))}"
    ]
    if j.web_url:
        out.append("  " + fmt.paint(j.web_url, "dim"))
    out += ["", fmt.paint(f"Releases comparados ({len(s.compared)})", "bold"), "  " + (", ".join(s.compared) or "—")]

    out += ["", fmt.paint("Objetos alterados", "bold")]
    if s.changes:
        table = fmt.Table(["ação", "namespace", "objeto", "kind"], "llll")
        for c in s.changes:
            table.add(c.action, c.namespace, c.name, c.kind + (f" ({c.group})" if c.group else ""))
        out.append(table.render())
    else:
        out.append("  " + ("nenhum (apply sem mudanças)" if s.no_changes else "—"))

    if s.releases:
        out += ["", fmt.paint("Releases", "bold")]
        table = fmt.Table(["tabela", "release", "namespace", "chart", "versão", "duração"], "llllrr")
        for row in s.releases:
            table.add(row.table, row.name, row.namespace or "—", _chart_name(row.chart), row.version or "—", row.duration or "—")
        out.append(table.render())

    if s.errors:
        out += ["", fmt.paint("Erros", "bold")] + [f"  {fmt.level_mark('erro')} {e}" for e in s.errors]
    if r.show_diff:
        for c in s.changes:
            out += ["", fmt.paint(f"Diff {c.namespace}/{c.name} {c.kind}", "bold")] + [f"  {line}" for line in c.diff]
    if r.redacted:
        out += ["", fmt.paint(f"  {r.redacted} trecho(s) redigido(s)", "dim")]
    return "\n".join(out)


def render_helmfile_markdown(r: HelmfileReport) -> str:
    j, s = r.job, r.summary
    title = f"[{j.name}]({j.web_url})" if j.web_url else j.name
    out = [f"## Job {j.id} — {title}", "", f"- **Status**: `{j.status_label}` · resultado `{s.result or '—'}`"]
    out.append(f"- **Releases comparados** ({len(s.compared)}): " + (", ".join(f"`{n}`" for n in s.compared) or "—"))
    if s.no_changes:
        out.append("- **Objetos alterados**: nenhum (apply sem mudanças)")
    elif s.changes:
        table = fmt.Table(["ação", "namespace", "objeto", "kind"], "llll")
        for c in s.changes:
            table.add(c.action, c.namespace, f"`{c.name}`", c.kind)
        out += ["", "### Objetos alterados", "", table.render_markdown()]
    if s.releases:
        table = fmt.Table(["tabela", "release", "namespace", "chart", "versão", "duração"], "llllrr")
        for row in s.releases:
            table.add(row.table, f"`{row.name}`", row.namespace or "—", _chart_name(row.chart), row.version or "—", row.duration or "—")
        out += ["", "### Releases", "", table.render_markdown()]
    if s.errors:
        out += ["", "### Erros", ""] + [f"- ❌ `{e}`" for e in s.errors]
    if r.show_diff:
        for c in s.changes:
            out += ["", f"### Diff `{c.namespace}/{c.name}` {c.kind}", "", "```diff", *c.diff, "```"]
    if r.redacted:
        out += ["", f"_{r.redacted} trecho(s) redigido(s)_"]
    return "\n".join(out)
