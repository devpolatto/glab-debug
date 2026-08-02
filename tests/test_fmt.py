from glab_debug import fmt


def setup_module():
    fmt.configure_color(False)


def test_duration():
    assert fmt.duration(None) == "—"
    assert fmt.duration(0.25) == "250ms"
    assert fmt.duration(21.84) == "21.8s"
    assert fmt.duration(665.3) == "11m05.3s"
    assert fmt.duration(3725.0) == "1h02m05.0s"


def test_percent_e_seconds():
    assert fmt.percent(0.62) == "62.0%"
    assert fmt.percent(None) == "—"
    assert fmt.seconds(665.298) == "665.3"


def test_timeline_bar_respeita_a_largura():
    bar, start = fmt.timeline_bar(offset=50, length=50, total=100, width=10)
    assert start == 5
    assert bar == "     █████"
    assert len(bar) == 10


def test_timeline_bar_job_curto_ainda_aparece():
    bar, _ = fmt.timeline_bar(offset=0, length=0.1, total=1000, width=20)
    assert bar == "█"


def test_tabela_markdown_escapa_pipe():
    table = fmt.Table(["a", "b"], "lr")
    table.add("x|y", "1")
    assert "x\\|y" in table.render_markdown()
