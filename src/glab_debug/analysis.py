"""Derivação dos números do pipeline a partir dos payloads crus da API."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from fnmatch import fnmatchcase

from .models import (
    Alert,
    Job,
    JobReport,
    MergeRequest,
    Pipeline,
    PipelineReport,
    Project,
    StageReport,
    StatusJob,
    StatusReport,
    Timing,
    as_seconds,
)

#: Acima disto, o tempo em fila deixa de ser ruído e vira aviso.
QUEUE_WARN_SECONDS = 30.0
#: Fração do pipeline ocupada por um único job para chamá-lo de dominante.
DOMINANT_SHARE = 0.5
#: Fração do pipeline sem nenhum job executando que merece aviso.
IDLE_WARN_SHARE = 0.10

_UNFINISHED = {"created", "manual", "scheduled", "skipped", "pending", "waiting_for_resource"}

#: Estados de job que entram na lista do `status` sem filtro. `manual`, `created`,
#: `scheduled` e `skipped` viram só contagem: em pipeline GitOps, são a maioria e não dizem
#: nada sobre o que rodou.
INFORMATIVE = {
    "failed", "running", "pending", "preparing", "waiting_for_resource",
    "canceled", "canceling", "success",
}

#: Estados em que a pipeline (ou o job) ainda vai mudar sozinha. `manual` não está aqui de
#: propósito: é onde as pipelines GitOps param, e para quem espera é um estado final.
IN_PROGRESS = {"created", "waiting_for_resource", "preparing", "pending", "running", "canceling"}


def analyse_pipeline(
    project: Project,
    pipeline: Pipeline,
    jobs: list[Job],
    include_retried: bool = False,
) -> PipelineReport:
    jobs = sorted(jobs, key=lambda j: (j.started_at or j.created_at or _EPOCH, j.id))

    end, estimated = _resolve_end(pipeline, jobs)
    wall = as_seconds(end - pipeline.created_at)
    origin = pipeline.created_at

    attempts = _count_attempts(jobs)
    seen: dict[tuple[str, str], int] = defaultdict(int)

    reports: list[JobReport] = []
    for job in jobs:
        key = (job.stage, job.name)
        seen[key] += 1
        offset = as_seconds(job.started_at - origin) if job.started_at else None
        share = (job.duration / wall) if (job.duration and wall > 0) else None
        reports.append(
            JobReport(
                id=job.id,
                name=job.name,
                stage=job.stage,
                status=job.status,
                allow_failure=job.allow_failure,
                failure_reason=job.failure_reason,
                duration=job.duration,
                queued=job.queued_duration,
                started_at=job.started_at,
                finished_at=job.finished_at,
                offset=offset,
                share=share,
                runner_id=job.runner.id if job.runner else None,
                runner_description=job.runner.description if job.runner else None,
                attempt=seen[key],
                attempts_total=attempts[key],
                web_url=job.web_url,
            )
        )

    busy = _union_seconds(
        [(j.started_at, j.finished_at) for j in jobs if j.started_at and j.finished_at]
    )
    jobs_sum = sum(j.duration or 0.0 for j in jobs)
    queued_sum = sum(j.queued_duration or 0.0 for j in jobs)

    timing = Timing(
        created_at=pipeline.created_at,
        started_at=pipeline.started_at,
        finished_at=end,
        finished_estimated=estimated,
        wall=wall,
        api_duration=pipeline.duration,
        pipeline_queued=pipeline.queued_duration,
        jobs_sum=jobs_sum,
        jobs_queued_sum=queued_sum,
        busy=busy,
        idle=max(wall - busy, 0.0),
        parallelism=(jobs_sum / busy) if busy > 0 else 0.0,
    )

    stages = _build_stages(reports, wall)
    alerts = _build_alerts(pipeline, reports, timing, include_retried)

    return PipelineReport(
        project=project,
        pipeline=pipeline,
        commit=next((j.commit for j in jobs if j.commit), None),
        timing=timing,
        jobs=reports,
        stages=stages,
        alerts=alerts,
        include_retried=include_retried,
    )


# --------------------------------------------------------------------------- #
# Auxiliares
# --------------------------------------------------------------------------- #

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _resolve_end(pipeline: Pipeline, jobs: list[Job]) -> tuple[datetime, bool]:
    """Fim do pipeline. Quando a API não dá, infere do último job concluído."""
    if pipeline.finished_at:
        return pipeline.finished_at, False
    finishes = [j.finished_at for j in jobs if j.finished_at]
    if finishes:
        return max(finishes), True
    return datetime.now(timezone.utc), True


def _count_attempts(jobs: list[Job]) -> dict[tuple[str, str], int]:
    counter: dict[tuple[str, str], int] = defaultdict(int)
    for job in jobs:
        counter[(job.stage, job.name)] += 1
    return counter


def _union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    """Soma dos intervalos após mesclar sobreposições — o tempo em que algo rodava."""
    if not intervals:
        return 0.0
    ordered = sorted(intervals, key=lambda p: p[0])
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start > cur_end:
            total += as_seconds(cur_end - cur_start)
            cur_start, cur_end = start, end
        elif end > cur_end:
            cur_end = end
    total += as_seconds(cur_end - cur_start)
    return total


def _build_stages(jobs: list[JobReport], wall: float) -> list[StageReport]:
    grouped: dict[str, list[JobReport]] = defaultdict(list)
    for job in jobs:
        grouped[job.stage].append(job)

    stages: list[StageReport] = []
    for name, items in grouped.items():
        starts = [j.started_at for j in items if j.started_at]
        ends = [j.finished_at for j in items if j.finished_at]
        span = as_seconds(max(ends) - min(starts)) if starts and ends else None
        ran = [j.duration for j in items if j.duration]
        total = sum(ran) if ran else None
        stages.append(
            StageReport(
                name=name,
                jobs=len(items),
                duration_sum=total,
                span=span,
                started_at=min(starts) if starts else None,
                finished_at=max(ends) if ends else None,
                share=(total / wall) if (total and wall > 0) else None,
                failed=sum(1 for j in items if j.status == "failed"),
            )
        )

    stages.sort(key=lambda s: (s.started_at or datetime.max.replace(tzinfo=timezone.utc), s.name))
    return stages


def _build_alerts(
    pipeline: Pipeline,
    jobs: list[JobReport],
    timing: Timing,
    include_retried: bool,
) -> list[Alert]:
    alerts: list[Alert] = []

    for job in jobs:
        if job.status != "failed":
            continue
        reason = f" ({job.failure_reason})" if job.failure_reason else ""
        if job.allow_failure:
            alerts.append(
                Alert(
                    level="aviso",
                    message=f"{job.stage}/{job.name} falhou{reason}, mas é allow_failure.",
                )
            )
        else:
            alerts.append(
                Alert(level="erro", message=f"{job.stage}/{job.name} falhou{reason} — job {job.id}.")
            )

    retried = sorted({f"{j.stage}/{j.name}" for j in jobs if j.is_retry})
    if retried:
        alerts.append(
            Alert(
                level="aviso",
                message=(
                    f"Job reexecutado: {', '.join(retried)}. O tempo somado inclui as tentativas "
                    "descartadas."
                ),
            )
        )
    elif not include_retried:
        alerts.append(
            Alert(
                level="info",
                message=(
                    "Tentativas anteriores não consultadas — use --retried para incluir jobs "
                    "que foram reexecutados."
                ),
            )
        )

    for job in jobs:
        if job.queued and job.queued > QUEUE_WARN_SECONDS:
            alerts.append(
                Alert(
                    level="aviso",
                    message=(
                        f"{job.stage}/{job.name} esperou {job.queued:.0f}s na fila antes de "
                        "iniciar — indício de runner ocupado."
                    ),
                )
            )

    dominant = max((j for j in jobs if j.share), key=lambda j: j.share or 0, default=None)
    if dominant and (dominant.share or 0) >= DOMINANT_SHARE:
        alerts.append(
            Alert(
                level="info",
                message=(
                    f"{dominant.stage}/{dominant.name} concentra "
                    f"{(dominant.share or 0) * 100:.0f}% do tempo total — é onde otimizar rende."
                ),
            )
        )

    if timing.wall > 0 and timing.idle / timing.wall >= IDLE_WARN_SHARE:
        alerts.append(
            Alert(
                level="aviso",
                message=(
                    f"{timing.idle:.0f}s ({timing.idle / timing.wall * 100:.0f}%) do pipeline sem "
                    "nenhum job executando — fila, aprovação manual ou overhead de runner."
                ),
            )
        )

    if pipeline.status in _UNFINISHED or timing.finished_estimated:
        alerts.append(
            Alert(
                level="info",
                message=(
                    f"Pipeline em `{pipeline.status}`: a API não expõe finished_at, o término foi "
                    "inferido do último job concluído."
                ),
            )
        )

    return alerts


# --------------------------------------------------------------------------- #
# pipeline status
# --------------------------------------------------------------------------- #


def job_matches(name: str, globs: list[str]) -> bool:
    return any(fnmatchcase(name, g) for g in globs)


def still_running(pipeline: Pipeline, jobs: list[Job], globs: list[str]) -> bool:
    """Critério do `--wait`: com filtro, olha só os jobs filtrados; sem filtro, a pipeline."""
    if globs:
        chosen = [j for j in jobs if job_matches(j.name, globs)]
        if chosen:
            return any(j.status in IN_PROGRESS for j in chosen)
    return pipeline.status in IN_PROGRESS


def analyse_status(
    project: Project,
    pipeline: Pipeline,
    jobs: list[Job],
    globs: list[str] | None = None,
    show_all: bool = False,
    via: str | None = None,
    merge_request: MergeRequest | None = None,
) -> StatusReport:
    globs = globs or []
    ordered = sorted(jobs, key=lambda j: (j.started_at or j.created_at or _EPOCH, j.id))

    if globs:
        chosen = [j for j in ordered if job_matches(j.name, globs)]
    elif show_all:
        chosen = ordered
    else:
        chosen = [j for j in ordered if j.status in INFORMATIVE]

    listed = [
        StatusJob(
            id=j.id,
            name=j.name,
            stage=j.stage,
            status=j.status,
            allow_failure=j.allow_failure,
            failure_reason=j.failure_reason,
            duration=j.duration,
            web_url=j.web_url,
        )
        for j in chosen
    ]

    alerts: list[Alert] = []
    for job in listed:
        if job.status != "failed":
            continue
        reason = f" ({job.failure_reason})" if job.failure_reason else ""
        if job.allow_failure:
            alerts.append(Alert(level="aviso", message=f"{job.stage}/{job.name} falhou{reason}, mas é allow_failure."))
        else:
            alerts.append(Alert(level="erro", message=f"{job.stage}/{job.name} falhou{reason} — job {job.id}."))

    if pipeline.status == "failed" and not any(j.status == "failed" for j in jobs):
        detail = f": {pipeline.yaml_errors}" if pipeline.yaml_errors else ""
        alerts.append(
            Alert(
                level="erro",
                message=(
                    f"Pipeline falhou sem nenhum job falho{detail} — erro na criação "
                    "(YAML, rules, include ou permissão). Detalhe só na UI."
                ),
            )
        )

    if globs and not listed:
        alerts.append(Alert(level="aviso", message=f"Nenhum job bate com o filtro: {', '.join(globs)}."))
    if via == "head_pipeline":
        alerts.append(
            Alert(
                level="aviso",
                message="MR não mergeado: esta é a pipeline do MR (head_pipeline), não a do branch alvo.",
            )
        )

    return StatusReport(
        project=project,
        pipeline=pipeline,
        via=via,
        merge_request=merge_request,
        counts=dict(sorted(Counter(j.status for j in jobs).items(), key=lambda kv: (-kv[1], kv[0]))),
        jobs=listed,
        hidden=len(jobs) - len(listed),
        filters=globs,
        alerts=alerts,
    )
