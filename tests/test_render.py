from argparse import Namespace

from glab_debug import fmt
from glab_debug.analysis import analyse_pipeline
from glab_debug.commands.pipeline import render_compact, render_markdown, render_table
from test_analysis import PROJECT, make_job, make_pipeline


def setup_module():
    fmt.configure_color(False)


def args(**kw):
    base = dict(sort="duration", top=None, no_timeline=False, no_stages=False)
    base.update(kw)
    return Namespace(**base)


def build_report(n=5):
    jobs = [
        make_job(f"job{i}", "build" if i < 2 else "test", start=i * 10, dur=10 * (n - i), job_id=i)
        for i in range(n)
    ]
    return analyse_pipeline(PROJECT, make_pipeline(), jobs)


def test_compact_tem_uma_linha_por_job_e_cabecalho_unico():
    report = build_report()
    lines = render_compact(report, args()).splitlines()

    assert lines[0].startswith("pipeline 58185")
    assert lines[1].startswith("timing wall=")
    assert lines[2] == "jobs stage/name status dur queued pct runner"
    job_lines = [line for line in lines if line.startswith(("build/", "test/"))]
    assert len(job_lines) == len(report.jobs)


def test_compact_omite_alertas_informativos():
    report = build_report()
    assert any(a.level == "info" for a in report.alerts)  # o report tem info…
    out = render_compact(report, args())
    assert not any(line.startswith("info ") for line in out.splitlines())  # …e o compact não


def test_compact_e_bem_menor_que_a_tabela():
    report = build_report()
    assert len(render_compact(report, args())) < len(render_table(report, args())) / 2


def test_top_limita_os_jobs_e_resume_o_resto():
    report = build_report(n=5)
    lines = render_compact(report, args(top=2)).splitlines()

    job_lines = [line for line in lines if line.startswith(("build/", "test/"))]
    assert len(job_lines) == 2
    resumo = next(line for line in lines if line.startswith("+"))
    assert resumo == "+3 jobs omitidos soma=60.0s"  # 30 + 20 + 10


def test_top_maior_que_o_total_nao_resume():
    out = render_compact(build_report(n=3), args(top=99))
    assert "omitidos" not in out


def test_top_vale_para_table_e_markdown():
    report = build_report(n=5)
    assert "+3 jobs omitidos" in render_table(report, args(top=2))
    assert "+3 jobs omitidos" in render_markdown(report, args(top=2))
