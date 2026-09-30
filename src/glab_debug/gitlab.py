"""Camada de acesso ao GitLab.

Toda chamada passa pelo `glab api`, e não por HTTP direto, por um motivo prático:
o `glab` já está autenticado na máquina, então a ferramenta não precisa guardar,
ler nem rotacionar token nenhum.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any
from urllib.parse import quote, urlparse

from pydantic import TypeAdapter

from .config import Settings
from .models import Job, MergeRequest, Pipeline, Project


class GlabError(RuntimeError):
    """Falha ao conversar com o GitLab através do glab."""


def project_ref(project: str) -> str:
    """Normaliza projeto (ID, path ou URL) para o formato aceito na rota da API.

    >>> project_ref("621")
    '621'
    >>> project_ref("https://gitlab.exemplo/cix/apps/admin-api/-/pipelines/1")
    'cix%2Fapps%2Fadmin-api'
    """
    ref = project.strip()
    if ref.startswith(("http://", "https://")):
        ref = urlparse(ref).path
    ref = ref.split("/-/", 1)[0].strip("/")
    if not ref:
        raise GlabError(f"Não consegui extrair o projeto de {project!r}.")
    if ref.isdigit():
        return ref
    return quote(ref, safe="")


def infer_project_from_git(cwd: str | None = None) -> str | None:
    """Tenta descobrir o projeto pelo remote git do diretório atual."""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None

    url = out.stdout.strip()
    if not url:
        return None
    if url.startswith("git@"):  # git@host:grupo/projeto.git
        _, _, path = url.partition(":")
    elif url.startswith(("http://", "https://", "ssh://")):
        path = urlparse(url).path
    else:
        return None
    path = path.strip("/").removesuffix(".git")
    return path or None


class GitLabClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if shutil.which(settings.glab_bin) is None:
            raise GlabError(
                f"Executável {settings.glab_bin!r} não encontrado no PATH. "
                "Instale o glab e autentique com `glab auth login`."
            )

    # ---------------------------------------------------------------- baixo nível

    def _run(self, path: str) -> str:
        cmd = [self.settings.glab_bin, "api", path]
        if self.settings.debug:
            print(f"[glab-debug] GITLAB_HOST={self.settings.host} {' '.join(cmd)}", file=sys.stderr)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.settings.timeout,
                env={**_base_env(), "GITLAB_HOST": self.settings.host},
            )
        except subprocess.TimeoutExpired as exc:
            raise GlabError(f"Timeout de {self.settings.timeout}s em GET {path}") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            msg = next((line.strip() for line in detail if line.strip()), "erro desconhecido")
            raise GlabError(f"GET {path} falhou: {msg.removeprefix('glab: ')}")
        return proc.stdout

    def get(self, path: str, **params: Any) -> Any:
        return json.loads(self._run(_with_params(path, params)) or "null")

    def get_text(self, path: str) -> str:
        """Resposta crua, para rotas que não devolvem JSON (trace de job)."""
        return self._run(path)

    def get_all(self, path: str, **params: Any) -> list[Any]:
        """Pagina manualmente — o resultado do `--paginate` do glab não é um JSON único."""
        items: list[Any] = []
        per_page = self.settings.per_page
        for page in range(1, self.settings.max_pages + 1):
            batch = self.get(path, per_page=per_page, page=page, **params)
            if not isinstance(batch, list):
                raise GlabError(f"GET {path} não devolveu uma lista.")
            items.extend(batch)
            if len(batch) < per_page:
                break
        return items

    # ---------------------------------------------------------------- alto nível

    def project(self, project: str) -> Project:
        return Project.model_validate(self.get(f"projects/{project_ref(project)}"))

    def pipeline(self, project: str, pipeline_id: int) -> Pipeline:
        payload = self.get(f"projects/{project_ref(project)}/pipelines/{pipeline_id}")
        return Pipeline.model_validate(payload)

    def pipeline_jobs(
        self, project: str, pipeline_id: int, include_retried: bool = False
    ) -> list[Job]:
        path = f"projects/{project_ref(project)}/pipelines/{pipeline_id}/jobs"
        params: dict[str, Any] = {}
        if include_retried:
            params["include_retried"] = "true"
        return TypeAdapter(list[Job]).validate_python(self.get_all(path, **params))

    def pipelines_by_sha(self, project: str, sha: str, ref: str | None = None) -> list[Pipeline]:
        """Pipelines de um commit, da mais recente para a mais antiga."""
        path = f"projects/{project_ref(project)}/pipelines"
        payload = self.get(path, sha=sha, ref=ref, order_by="id", sort="desc", per_page=20)
        return TypeAdapter(list[Pipeline]).validate_python(payload or [])

    def full_sha(self, project: str, sha: str) -> str:
        """O filtro `sha=` da API só aceita o SHA completo; expande o curto pelo commit."""
        if len(sha) == 40:
            return sha
        payload = self.get(f"projects/{project_ref(project)}/repository/commits/{quote(sha, safe='')}")
        return payload["id"]

    def latest_pipeline(self, project: str, ref: str) -> Pipeline:
        payload = self.get(f"projects/{project_ref(project)}/pipelines/latest", ref=ref)
        return Pipeline.model_validate(payload)

    def merge_request(self, project: str, iid: int) -> MergeRequest:
        payload = self.get(f"projects/{project_ref(project)}/merge_requests/{iid}")
        return MergeRequest.model_validate(payload)

    def job(self, project: str, job_id: int) -> Job:
        return Job.model_validate(self.get(f"projects/{project_ref(project)}/jobs/{job_id}"))

    def job_trace(self, project: str, job_id: int) -> str:
        return self.get_text(f"projects/{project_ref(project)}/jobs/{job_id}/trace")

    def pipeline_bridges(self, project: str, pipeline_id: int) -> list[dict[str, Any]]:
        path = f"projects/{project_ref(project)}/pipelines/{pipeline_id}/bridges"
        try:
            return self.get_all(path)
        except GlabError:
            return []  # projetos sem child pipelines podem responder 403/404


def _with_params(path: str, params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    if not clean:
        return path
    sep = "&" if "?" in path else "?"
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in clean.items())
    return f"{path}{sep}{query}"


def _base_env() -> dict[str, str]:
    import os

    return dict(os.environ)
