from datetime import datetime, timedelta, timezone

from glab_debug.analysis import _union_seconds, analyse_pipeline
from glab_debug.models import Job, Pipeline, Project

T0 = datetime(2026, 7, 30, 18, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make_job(name, stage, start, dur, status="success", job_id=1, **kw):
    return Job(
        id=job_id,
        name=name,
        stage=stage,
        status=status,
        created_at=T0,
        started_at=at(start),
        finished_at=at(start + dur),
        duration=float(dur),
        queued_duration=kw.pop("queued", 1.0),
        **kw,
    )


PROJECT = Project(id=621, name="admin-api", path_with_namespace="cix/apps/admin-api")


def make_pipeline(**kw):
    base = dict(
        id=58185,
        iid=511,
        status="success",
        created_at=T0,
        started_at=at(1),
        finished_at=at(100),
        duration=99.0,
        queued_duration=1.0,
    )
    base.update(kw)
    return Pipeline(**base)


def test_union_ignora_sobreposicao():
    assert _union_seconds([(at(0), at(10)), (at(5), at(20))]) == 20.0
    assert _union_seconds([(at(0), at(10)), (at(30), at(40))]) == 20.0
    assert _union_seconds([]) == 0.0


def test_numeros_basicos_e_ociosidade():
    jobs = [
        make_job("a", "build", start=10, dur=30, job_id=1),
        make_job("b", "test", start=60, dur=20, job_id=2),
    ]
    report = analyse_pipeline(PROJECT, make_pipeline(), jobs)
    t = report.timing

    assert t.wall == 100.0
    assert t.jobs_sum == 50.0
    assert t.busy == 50.0
    assert t.idle == 50.0  # 0→10, 40→60 e 80→100
    assert t.parallelism == 1.0
    assert [j.offset for j in report.jobs] == [10.0, 60.0]
    assert report.jobs[0].share == 0.3


def test_paralelismo_maior_que_um():
    jobs = [
        make_job("a", "test", start=0, dur=40, job_id=1),
        make_job("b", "test", start=0, dur=40, job_id=2),
    ]
    report = analyse_pipeline(PROJECT, make_pipeline(), jobs)
    assert report.timing.busy == 40.0
    assert report.timing.jobs_sum == 80.0
    assert report.timing.parallelism == 2.0


def test_stage_sem_job_executado_nao_soma_zero():
    jobs = [
        make_job("a", "build", start=0, dur=50, job_id=1),
        Job(id=2, name="notify", stage=".post", status="created"),
    ]
    report = analyse_pipeline(PROJECT, make_pipeline(), jobs)
    post = next(s for s in report.stages if s.name == ".post")
    assert post.duration_sum is None
    assert post.share is None


def test_tentativas_sao_numeradas():
    jobs = [
        make_job("build:arm64", "build", start=0, dur=715, status="failed", job_id=324417),
        make_job("build:arm64", "build", start=720, dur=681, job_id=324505),
    ]
    report = analyse_pipeline(PROJECT, make_pipeline(finished_at=at(1500)), jobs, include_retried=True)
    assert [(j.attempt, j.attempts_total) for j in report.jobs] == [(1, 2), (2, 2)]
    assert all(j.is_retry for j in report.jobs)
    assert any("reexecutado" in a.message.lower() for a in report.alerts)


def test_fim_inferido_quando_pipeline_nao_terminou():
    jobs = [make_job("a", "build", start=10, dur=30, job_id=1)]
    report = analyse_pipeline(PROJECT, make_pipeline(status="manual", finished_at=None), jobs)
    assert report.timing.finished_estimated is True
    assert report.timing.finished_at == at(40)
    assert report.timing.wall == 40.0


def test_job_falho_vira_alerta_de_erro():
    jobs = [make_job("trivy", "sast", start=0, dur=20, status="failed", job_id=1)]
    report = analyse_pipeline(PROJECT, make_pipeline(), jobs)
    assert any(a.level == "erro" and "trivy" in a.message for a in report.alerts)


def test_allow_failure_rebaixa_para_aviso():
    jobs = [
        make_job("sonar", "sast", start=0, dur=20, status="failed", job_id=1, allow_failure=True)
    ]
    report = analyse_pipeline(PROJECT, make_pipeline(), jobs)
    assert not any(a.level == "erro" for a in report.alerts)
    assert any(a.level == "aviso" and "sonar" in a.message for a in report.alerts)


def test_job_dominante_e_fila_alta():
    jobs = [
        make_job("build", "build", start=0, dur=90, job_id=1, queued=45.0),
        make_job("t", "test", start=90, dur=5, job_id=2),
    ]
    report = analyse_pipeline(PROJECT, make_pipeline(), jobs)
    messages = " ".join(a.message for a in report.alerts)
    assert "concentra" in messages
    assert "fila" in messages
