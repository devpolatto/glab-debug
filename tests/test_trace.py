from glab_debug.trace import MAX_LINES, clean, gaps, grep, tail

ESC = "\x1b"

# Formato real do runner 19.x (job 366779 do cix-app-helmfiles), com conteúdo sintético.
RAW = "\n".join(
    [
        f"2026-09-30T19:38:19.213893Z 00O {ESC}[0KRunning with gitlab-runner 19.1.0 (5eb085ab){ESC}[0;m",
        f"2026-09-30T19:38:19.213982Z 00O section_start:1790797099:prepare_executor\r{ESC}[0K",
        f"2026-09-30T19:38:19.213986Z 00O+{ESC}[0K{ESC}[36;1mPreparing the \"kubernetes\" executor{ESC}[0;m{ESC}[0;m",
        f"2026-09-30T19:38:19.262834Z 00O {ESC}[0KUsing Kubernetes executor with image helmfile:v0.171.0 ...{ESC}[0;m",
        f"2026-09-30T19:38:19.401883Z 00O section_end:1790797104:prepare_executor\r{ESC}[0K",
        f"2026-09-30T19:38:19.406823Z 00O+section_start:1790797104:step_script[collapsed=true]\r{ESC}[0K",
        "2026-09-30T19:38:20.000000Z 01O $ helmfile apply",
        "2026-09-30T19:38:21.000000Z 01O progresso 10%\rprogresso 100%",
        "2026-09-30T19:38:22.000000Z 01E Error: algo quebrou",
        f"2026-09-30T19:39:42.913156Z 00O section_end:1790797182:step_script\r{ESC}[0K",
        f"2026-09-30T19:39:42.913156Z 00O {ESC}[32;1mJob succeeded{ESC}[0;m",
        "",
    ]
)


def test_clean_remove_prefixo_ansi_e_marcadores():
    trace = clean(RAW)
    assert trace.lines == [
        "Running with gitlab-runner 19.1.0 (5eb085ab)",
        'Preparing the "kubernetes" executor',
        "Using Kubernetes executor with image helmfile:v0.171.0 ...",
        "$ helmfile apply",
        "progresso 100%",
        "Error: algo quebrou",
        "Job succeeded",
    ]


def test_clean_extrai_secoes_com_duracao_e_linhas():
    sections = {s.name: s for s in clean(RAW).sections}
    assert sections["prepare_executor"].duration == 5.0
    assert sections["prepare_executor"].start_line == 2
    assert sections["prepare_executor"].end_line == 3
    assert sections["step_script"].duration == 78.0
    assert sections["step_script"].start_line == 4


def test_clean_aceita_trace_sem_prefixo():
    assert clean("linha 1\n\x1b[31mlinha 2\x1b[0m\n").lines == ["linha 1", "linha 2"]


def test_tail_numera_e_respeita_o_teto():
    lines = [f"l{i}" for i in range(1, 1001)]
    ex = tail(lines, 5)
    assert ex.rows == [(996, "l996"), (997, "l997"), (998, "l998"), (999, "l999"), (1000, "l1000")]
    assert ex.total_lines == 1000 and ex.truncated == 0

    big = tail(lines, 5000)
    assert len(big.rows) == MAX_LINES
    assert big.truncated == 1000 - MAX_LINES
    assert big.rows[-1] == (1000, "l1000")


def test_grep_funde_janelas_e_conta_matches():
    lines = ["a", "ERRO 1", "b", "c", "ERRO 2", "d", "e", "f", "g", "ERRO 3"]
    ex = grep(lines, r"ERRO", context=1)
    assert [n for n, _ in ex.rows] == [1, 2, 3, 4, 5, 6, 9, 10]
    assert ex.matches == 3
    marked = gaps(ex.rows)
    assert marked.index(None) == 6  # salto entre a linha 6 e a 9
