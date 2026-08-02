import pytest

from glab_debug.gitlab import GlabError, _with_params, project_ref


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("621", "621"),
        ("cix/apps/admin-api", "cix%2Fapps%2Fadmin-api"),
        ("/cix/apps/admin-api/", "cix%2Fapps%2Fadmin-api"),
        ("https://gitlab.exemplo/cix/apps/admin-api", "cix%2Fapps%2Fadmin-api"),
        ("https://gitlab.exemplo/cix/apps/admin-api/-/pipelines/58185", "cix%2Fapps%2Fadmin-api"),
    ],
)
def test_project_ref(raw, expected):
    assert project_ref(raw) == expected


def test_project_ref_vazio():
    with pytest.raises(GlabError):
        project_ref("https://gitlab.exemplo/")


def test_with_params_ignora_none():
    assert _with_params("a/b", {"page": 1, "x": None}) == "a/b?page=1"
    assert _with_params("a/b", {}) == "a/b"
    assert _with_params("a/b?z=1", {"page": 2}) == "a/b?z=1&page=2"
