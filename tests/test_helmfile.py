from glab_debug.helmfile import parse

# Sintético, no formato do job 366779 (helmfile v0.171 + helm-diff).
APPLY = """\
$ helmfile --file clusters/africa/govone-v2-africa-prd apply
Pulling gru.ocir.io/x/cix/devops/helm-charts/govone-v2:~0.2.6
Decrypting secret /builds/cix/clusters/africa/govone-v2-africa-prd/admin-govone-africa/secrets-main.yaml
Comparing release=admin-govone-africa, chart=/tmp/helmfile1/master/admin/govone-v2/_0.2.6/govone-v2, namespace=master
Comparing release=portal-govone-africa, chart=/tmp/helmfile2/master/portal/govone-v2/_0.1.0/govone-v2, namespace=master
master, portal-govone-africa-portal, Deployment (apps) has changed:
  # Source: govone-v2/templates/main.yaml
  apiVersion: apps/v1
            - name: DB_PASSWORD
-             value: "antiga"
+             value: "nova"
+           livenessProbe:
+             httpGet:
+               path: /api/health
master, portal-govone-africa-cm, ConfigMap (v1) has been added:
+ data: {}
Upgrading release=portal-govone-africa, chart=/tmp/helmfile2/master/portal/govone-v2/_0.1.0/govone-v2, namespace=master
Release "portal-govone-africa" has been upgraded. Happy Helming!

UPDATED RELEASES:
NAME                   NAMESPACE   CHART                                   VERSION   DURATION
portal-govone-africa   master      oci://gru.ocir.io/x/helm-charts/govone-v2   0.1.7           1s

Comparing release=traefik, chart=traefik/traefik, namespace=traefik
Cleaning up project directory and file based variables
Job succeeded
""".splitlines()


def test_extrai_comparados_mudancas_releases_e_resultado():
    s = parse(APPLY)
    assert s.compared == ["admin-govone-africa", "portal-govone-africa", "traefik"]
    assert [(c.namespace, c.name, c.kind, c.group, c.action) for c in s.changes] == [
        ("master", "portal-govone-africa-portal", "Deployment", "apps", "changed"),
        ("master", "portal-govone-africa-cm", "ConfigMap", "v1", "added"),
    ]
    [release] = s.releases
    assert (release.table, release.name, release.namespace, release.version, release.duration) == (
        "UPDATED", "portal-govone-africa", "master", "0.1.7", "1s",
    )
    assert release.chart.endswith("govone-v2")
    assert s.result == "succeeded"
    assert not s.no_changes and s.errors == []


def test_corpo_do_diff_fica_so_no_objeto_e_para_na_fronteira():
    change = parse(APPLY).changes[0]
    assert change.diff[0] == "  # Source: govone-v2/templates/main.yaml"
    assert change.diff[-1] == "+               path: /api/health"
    assert all("Upgrading" not in line for line in parse(APPLY).changes[1].diff)


def test_apply_sem_mudanca_e_noop():
    lines = [
        "Comparing release=a, chart=x, namespace=master",
        "Comparing release=b, chart=y, namespace=master",
        "Job succeeded",
    ]
    s = parse(lines)
    assert s.no_changes and s.result == "succeeded"


def test_falha_coleta_erros_e_resultado():
    lines = [
        "Comparing release=a, chart=x, namespace=master",
        'Error: UPGRADE FAILED: another operation (install/upgrade/rollback) is in progress',
        "FAILED RELEASES:",
        "NAME   NAMESPACE   CHART   VERSION   DURATION",
        "a      master      x       0.1.0     3s",
        "",
        "ERROR: Job failed: command terminated with exit code 1",
    ]
    s = parse(lines)
    assert s.result == "failed"
    assert s.errors[0].startswith("Error: UPGRADE FAILED")
    assert [(r.table, r.name) for r in s.releases] == [("FAILED", "a")]
