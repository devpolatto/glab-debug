from argparse import Namespace

import pytest

from glab_debug import fmt
from glab_debug.analysis import analyse_status, still_running
from glab_debug.commands.pipeline import (
    render_status_compact,
    render_status_markdown,
    render_status_table,
    resolve_pipeline,
    status_exit_code,
)
from glab_debug.gitlab import GlabError
from glab_debug.models import MergeRequest, PipelineRef
from test_analysis import PROJECT, make_job, make_pipeline


def setup_module():
    fmt.configure_color(False)


def gitops_jobs():
    """Formato do cix-app-helmfiles: um deploy rodou, o resto ficou manual/created."""
    return [
        make_job("deploy:africa-prd", "deploy", start=5, dur=86.8, job_id=10),
        make_job("deploy:africa-hml", "deploy", start=0, dur=0, status="manual", job_id=11),
        make_job("sync:africa-prd", "deploy", start=0, dur=0, status="manual", job_id=12),
        make_job("notify:failure", ".post", start=0, dur=0, status="created", job_id=13),
        make_job("notify:skip", ".post", start=0, dur=0, status="skipped", job_id=14),
    ]


def test_sem_filtro_lista_so_estados_informativos():
    report = analyse_status(PROJECT, make_pipeline(status="manual"), gitops_jobs())
    assert [j.name for j in report.jobs] == ["deploy:africa-prd"]
    assert report.hidden == 4
    assert report.counts == {"manual": 2, "created": 1, "skipped": 1, "success": 1}
    assert status_exit_code(report) == 0


def test_filtro_mostra_o_job_em_qualquer_estado():
    report = analyse_status(PROJECT, make_pipeline(), gitops_jobs(), globs=["deploy:*"])
    assert {j.name: j.status for j in report.jobs} == {
        "deploy:africa-prd": "success",
        "deploy:africa-hml": "manual",
    }


def test_filtro_sem_match_gera_aviso():
    report = analyse_status(PROJECT, make_pipeline(), gitops_jobs(), globs=["build:*"])
    assert report.jobs == []
    assert any("Nenhum job bate" in a.message for a in report.alerts)


def test_all_lista_tudo():
    report = analyse_status(PROJECT, make_pipeline(), gitops_jobs(), show_all=True)
    assert len(report.jobs) == 5 and report.hidden == 0


def test_falha_da_exit_1_e_allow_failure_so_aviso():
    jobs = [
        make_job("build", "build", start=0, dur=10, status="failed", failure_reason="script_failure", job_id=1),
        make_job("lint", "test", start=0, dur=5, status="failed", allow_failure=True, job_id=2),
    ]
    report = analyse_status(PROJECT, make_pipeline(status="failed"), jobs)
    levels = sorted(a.level for a in report.alerts)
    assert levels == ["aviso", "erro"]
    assert status_exit_code(report) == 1
    assert "build/build failed 10.0 script_failure 1" in render_status_compact(report)
    assert "test/lint failed(allowed)" in render_status_compact(report)


def test_timeout_da_exit_3_mas_falha_prevalece():
    report = analyse_status(PROJECT, make_pipeline(status="running"), gitops_jobs())
    report.timed_out = True
    assert status_exit_code(report) == 3

    failed = analyse_status(
        PROJECT, make_pipeline(status="running"), [make_job("x", "build", 0, 1, status="failed", job_id=1)]
    )
    failed.timed_out = True
    assert status_exit_code(failed) == 1


def test_still_running_manual_e_estado_final():
    assert not still_running(make_pipeline(status="manual"), gitops_jobs(), [])
    assert still_running(make_pipeline(status="running"), gitops_jobs(), [])


def test_still_running_com_filtro_olha_so_os_jobs_filtrados():
    jobs = gitops_jobs() + [make_job("build:lento", "build", 0, 1, status="running", job_id=20)]
    running = make_pipeline(status="running")
    assert not still_running(running, jobs, ["deploy:africa-prd"])
    assert still_running(running, jobs, ["build:*"])


def test_compact_nao_perde_dado_do_table():
    report = analyse_status(PROJECT, make_pipeline(status="manual"), gitops_jobs(), globs=["deploy:*"])
    compact, table = render_status_compact(report), render_status_table(report)
    for job in report.jobs:
        assert str(job.id) in compact and str(job.id) in table
        assert job.name in compact and job.name in table
    for status, n in report.counts.items():
        assert f"{status}={n}" in compact
        assert f"{status} {n}" in table
    assert "deploy:africa-prd" in render_status_markdown(report)


# --------------------------------------------------------------------------- #
# Resolução do alvo
# --------------------------------------------------------------------------- #


class FakeClient:
    def __init__(self, mr=None, by_sha=None):
        self.mr = mr
        self.by_sha = by_sha or {}
        self.calls = []

    def merge_request(self, project, iid):
        return self.mr

    def pipelines_by_sha(self, project, sha, ref=None):
        self.calls.append((sha, ref))
        return self.by_sha.get(sha, [])

    def pipeline(self, project, pipeline_id):
        return make_pipeline(id=pipeline_id)

    def latest_pipeline(self, project, ref):
        return make_pipeline(id=999, ref=ref)


def target(**kw):
    base = dict(pipeline_id=None, sha=None, ref=None, mr=None)
    base.update(kw)
    return Namespace(**base)


def mr(**kw):
    base = dict(iid=50, state="merged", target_branch="main", sha="head123")
    base.update(kw)
    return MergeRequest(**base)


def test_mr_mergeado_usa_o_merge_commit_no_branch_alvo():
    client = FakeClient(mr=mr(merge_commit_sha="merge456"), by_sha={"merge456": [make_pipeline(id=67511)]})
    pipeline, via, found = resolve_pipeline(client, "p", target(mr=50))
    assert (pipeline.id, via, found.iid) == (67511, "merge_commit", 50)
    assert client.calls == [("merge456", "main")]


def test_mr_mergeado_por_fast_forward_usa_o_head():
    client = FakeClient(mr=mr(), by_sha={"head123": [make_pipeline(id=7)]})
    pipeline, via, _ = resolve_pipeline(client, "p", target(mr=50))
    assert (pipeline.id, via) == (7, "merge_commit")


def test_mr_aberto_usa_head_pipeline_e_avisa():
    client = FakeClient(mr=mr(state="opened", head_pipeline=PipelineRef(id=42)))
    pipeline, via, found = resolve_pipeline(client, "p", target(mr=50))
    assert (pipeline.id, via) == (42, "head_pipeline")
    report = analyse_status(PROJECT, pipeline, [], via=via, merge_request=found)
    assert any("não mergeado" in a.message for a in report.alerts)


def test_mr_mergeado_sem_pipeline_explica():
    client = FakeClient(mr=mr(merge_commit_sha="merge456"))
    with pytest.raises(GlabError, match="nenhum pipeline"):
        resolve_pipeline(client, "p", target(mr=50))


def test_sha_e_ref():
    client = FakeClient(by_sha={"abc": [make_pipeline(id=5), make_pipeline(id=4)]})
    assert resolve_pipeline(client, "p", target(sha="abc"))[0].id == 5
    assert resolve_pipeline(client, "p", target(ref="main"))[0].id == 999
    with pytest.raises(GlabError):
        resolve_pipeline(client, "p", target(sha="nada"))


def test_pipeline_falha_sem_job_falho_e_erro():
    report = analyse_status(PROJECT, make_pipeline(status="failed"), [])
    assert status_exit_code(report) == 1
    assert "sem nenhum job falho" in render_status_compact(report)

    with_yaml = analyse_status(PROJECT, make_pipeline(status="failed", yaml_errors="jobs:x config key may not be used"), [])
    assert "config key may not be used" in with_yaml.alerts[0].message
