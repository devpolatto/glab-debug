from argparse import Namespace

import pytest

from glab_debug import fmt
from glab_debug.commands.job import (
    build_helmfile,
    build_log,
    render_helmfile_compact,
    render_helmfile_markdown,
    render_helmfile_table,
    render_log_compact,
    render_log_markdown,
    render_log_table,
    select_jobs,
)
from glab_debug.gitlab import GlabError
from test_analysis import make_job

SECRETS = ("s3nh4-antiga", "s3nh4-nova", "pw-rabbit", "AKIAW3MD7PI2HI4YJRMF")

# Trace cru, com prefixo do runner, de um apply cujo diff expõe env com credencial.
RAW = "\n".join(
    f"2026-09-30T19:38:{i:02d}.000000Z 01O {line}"
    for i, line in enumerate(
        [
            "Comparing release=admin, chart=/tmp/h/govone-v2, namespace=hml",
            "hml, admin, Deployment (apps) has changed:",
            "            - name: DB_PASSWORD",
            '-             value: "s3nh4-antiga"',
            '+             value: "s3nh4-nova"',
            "            - name: RABBITMQ_URL",
            "              value: amqp://user:pw-rabbit@rabbitmq:5672",
            "            - name: AWS_ACCESS_KEY_ID",
            "              value: AKIAW3MD7PI2HI4YJRMF",
            "UPDATED RELEASES:",
            "NAME    NAMESPACE   CHART      VERSION   DURATION",
            "admin   hml         govone-v2  0.2.8     3m24s",
            "",
            "Job succeeded",
        ]
    )
)


def setup_module():
    fmt.configure_color(False)


def log_args(**kw):
    base = dict(sections=False, grep=None, tail=80, context=3)
    base.update(kw)
    return Namespace(**base)


def assert_clean(text: str):
    for secret in SECRETS:
        assert secret not in text, f"vazou {secret!r}"


JOB = make_job("deploy:hml", "deploy", start=0, dur=10, job_id=366623)


@pytest.mark.parametrize("render", [render_helmfile_compact, render_helmfile_table, render_helmfile_markdown])
def test_helmfile_diff_sai_redigido_em_todos_os_formatos(render):
    report = build_helmfile(JOB, RAW, show_diff=True)
    text = render(report)
    assert_clean(text)
    assert "<redacted" in text
    assert report.redacted >= 4
    assert report.summary.changes[0].name == "admin"


def test_helmfile_sem_diff_nao_carrega_corpo():
    report = build_helmfile(JOB, RAW, show_diff=False)
    assert all(c.diff == [] for c in report.summary.changes)
    assert_clean(report.model_dump_json())
    assert "updated admin ns=hml chart=govone-v2 version=0.2.8" in render_helmfile_compact(report)


@pytest.mark.parametrize("render", [render_log_compact, render_log_table, render_log_markdown])
def test_log_sai_redigido_em_todos_os_formatos(render):
    report = build_log(JOB, RAW, log_args())
    text = render(report)
    assert_clean(text)
    assert report.redacted >= 4


def test_log_json_tambem_redigido():
    assert_clean(build_log(JOB, RAW, log_args()).model_dump_json())


def test_grep_nao_acha_o_valor_cru_porque_busca_no_texto_redigido():
    report = build_log(JOB, RAW, log_args(grep="s3nh4"))
    assert report.matches == 0


# --------------------------------------------------------------------------- #
# Seleção de jobs
# --------------------------------------------------------------------------- #


class FakeClient:
    def __init__(self, jobs):
        self.jobs = jobs

    def job(self, project, job_id):
        return next(j for j in self.jobs if j.id == job_id)

    def pipeline_jobs(self, project, pipeline_id):
        return self.jobs


def sel(**kw):
    base = dict(job_id=None, pipeline=None, jobs=[], failed=False)
    base.update(kw)
    return Namespace(**base)


PIPE = [
    make_job("build", "build", 0, 10, job_id=1),
    make_job("test", "test", 10, 5, status="failed", job_id=2),
    make_job("deploy:hml", "deploy", 15, 5, job_id=3),
]


def test_selecao_por_id_por_glob_e_por_falha():
    client = FakeClient(PIPE)
    assert [j.id for j in select_jobs(client, "p", sel(job_id=3))] == [3]
    assert [j.id for j in select_jobs(client, "p", sel(pipeline=9, jobs=["deploy:*"]))] == [3]
    assert [j.id for j in select_jobs(client, "p", sel(pipeline=9, failed=True))] == [2]


def test_selecao_invalida_explica():
    client = FakeClient(PIPE)
    with pytest.raises(GlabError, match="Informe"):
        select_jobs(client, "p", sel(pipeline=9))
    with pytest.raises(GlabError, match="sozinho"):
        select_jobs(client, "p", sel(job_id=3, pipeline=9))
    with pytest.raises(GlabError, match="Nenhum job"):
        select_jobs(client, "p", sel(pipeline=9, jobs=["nada"]))
