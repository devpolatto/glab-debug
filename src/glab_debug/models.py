"""Modelos pydantic: payloads da API do GitLab e o resultado das análises.

Os modelos de payload usam `extra="ignore"` de propósito — a API do GitLab devolve
muito campo que não interessa aqui, e novos campos não devem quebrar a ferramenta.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .helmfile import HelmfileSummary
from .trace import Section

AlertLevel = Literal["erro", "aviso", "info"]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------- #
# Payloads da API
# --------------------------------------------------------------------------- #


class User(_Payload):
    name: str | None = None
    username: str | None = None


class Runner(_Payload):
    id: int
    description: str | None = None
    runner_type: str | None = None
    is_shared: bool | None = None


class Commit(_Payload):
    short_id: str | None = None
    title: str | None = None
    web_url: str | None = None


class Project(_Payload):
    id: int
    name: str
    path_with_namespace: str
    web_url: str | None = None
    default_branch: str | None = None


class Pipeline(_Payload):
    id: int
    iid: int | None = None
    project_id: int | None = None
    sha: str | None = None
    ref: str | None = None
    status: str
    source: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration: float | None = None
    queued_duration: float | None = None
    web_url: str | None = None
    user: User | None = None
    yaml_errors: str | None = None


class PipelineRef(_Payload):
    id: int
    status: str | None = None
    web_url: str | None = None


class MergeRequest(_Payload):
    iid: int
    title: str | None = None
    state: str
    source_branch: str | None = None
    target_branch: str | None = None
    sha: str | None = None
    merge_commit_sha: str | None = None
    squash_commit_sha: str | None = None
    merged_at: datetime | None = None
    head_pipeline: PipelineRef | None = None
    web_url: str | None = None


class Job(_Payload):
    id: int
    name: str
    stage: str
    status: str
    allow_failure: bool = False
    failure_reason: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration: float | None = None
    queued_duration: float | None = None
    runner: Runner | None = None
    web_url: str | None = None
    commit: Commit | None = None


# --------------------------------------------------------------------------- #
# Resultado da análise
# --------------------------------------------------------------------------- #


class JobReport(BaseModel):
    """Um job já com os números derivados que interessam ao diagnóstico."""

    id: int
    name: str
    stage: str
    status: str
    allow_failure: bool = False
    failure_reason: str | None = None
    duration: float | None = None
    queued: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    offset: float | None = Field(
        default=None, description="Segundos entre o início do pipeline e o início do job."
    )
    share: float | None = Field(
        default=None, description="Fração do tempo total do pipeline ocupada por este job."
    )
    runner_id: int | None = None
    runner_description: str | None = None
    attempt: int = Field(default=1, description="Ordem da tentativa quando o job foi reexecutado.")
    attempts_total: int = Field(default=1, description="Total de tentativas deste nome de job.")
    web_url: str | None = None

    @property
    def is_retry(self) -> bool:
        return self.attempts_total > 1


class StageReport(BaseModel):
    name: str
    jobs: int
    duration_sum: float | None
    span: float | None = Field(
        default=None, description="Do início do primeiro job ao fim do último job do stage."
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None
    share: float | None = None
    failed: int = 0


class Timing(BaseModel):
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    finished_estimated: bool = Field(
        default=False,
        description="True quando o pipeline não tem finished_at e o fim foi inferido dos jobs.",
    )
    wall: float = Field(description="Relógio de parede: criação → término.")
    api_duration: float | None = Field(
        default=None, description="`duration` reportado pela API (só execução)."
    )
    pipeline_queued: float | None = None
    jobs_sum: float = Field(default=0.0, description="Soma das durações de todos os jobs.")
    jobs_queued_sum: float = Field(default=0.0, description="Soma do tempo em fila dos jobs.")
    busy: float = Field(default=0.0, description="União dos intervalos de execução dos jobs.")
    idle: float = Field(default=0.0, description="Tempo do pipeline sem nenhum job executando.")
    parallelism: float = Field(
        default=0.0, description="jobs_sum / busy — 1.0 significa execução estritamente serial."
    )


class Alert(BaseModel):
    level: AlertLevel
    message: str


class PipelineReport(BaseModel):
    """Saída completa de `pipeline monitor`."""

    project: Project
    pipeline: Pipeline
    commit: Commit | None = None
    timing: Timing
    jobs: list[JobReport]
    stages: list[StageReport]
    alerts: list[Alert]
    include_retried: bool = False

    @property
    def slowest(self) -> JobReport | None:
        ran = [j for j in self.jobs if j.duration]
        return max(ran, key=lambda j: j.duration or 0) if ran else None


def as_seconds(delta: timedelta) -> float:
    return max(delta.total_seconds(), 0.0)


# --------------------------------------------------------------------------- #
# pipeline status
# --------------------------------------------------------------------------- #


class StatusJob(BaseModel):
    id: int
    name: str
    stage: str
    status: str
    allow_failure: bool = False
    failure_reason: str | None = None
    duration: float | None = None
    web_url: str | None = None

    @property
    def status_label(self) -> str:
        return f"{self.status}(allowed)" if self.status == "failed" and self.allow_failure else self.status


class StatusReport(BaseModel):
    """Saída de `pipeline status`: em que pé está a pipeline e quais jobs importam."""

    project: Project
    pipeline: Pipeline
    via: str | None = Field(
        default=None,
        description="Como a pipeline foi resolvida a partir de um MR: merge_commit ou head_pipeline.",
    )
    merge_request: MergeRequest | None = None
    counts: dict[str, int]
    jobs: list[StatusJob]
    hidden: int = Field(default=0, description="Jobs fora da lista (manual/created/skipped ou fora do filtro).")
    filters: list[str] = Field(default_factory=list)
    alerts: list[Alert]
    waited: float | None = Field(default=None, description="Segundos aguardando com --wait.")
    timed_out: bool = False


# --------------------------------------------------------------------------- #
# job log / job helmfile
# --------------------------------------------------------------------------- #


class LogReport(BaseModel):
    """Trecho do log de um job, já limpo e redigido."""

    job: StatusJob
    mode: str = Field(description="tail | grep | sections")
    pattern: str | None = None
    total_lines: int
    rows: list[tuple[int, str]] = Field(default_factory=list)
    truncated: int = 0
    matches: int | None = None
    sections: list[Section] = Field(default_factory=list)
    redacted: int = 0


class HelmfileReport(BaseModel):
    job: StatusJob
    summary: HelmfileSummary
    show_diff: bool = False
    redacted: int = 0


class JobsOutput(BaseModel):
    """Envelope do formato json: um relatório por job selecionado."""

    project: Project
    reports: list[LogReport] | list[HelmfileReport]
