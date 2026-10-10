"""Unit tests for the Repo Onboarding Agent (faultline.onboarding.source + .agent).

Everything here is offline: no network, no git, no Docker, no LLM. Repositories are fake trees
built under `tmp_path` and the model is a `StubLLM` that returns whatever the test hands it.

The centre of gravity is `agent.validate`: that function is the "code decides" boundary, so each
guard it implements gets its own assertion.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from faultline.onboarding import agent as A
from faultline.onboarding import source as S
from faultline.schemas import OnboardingReport, RepoKnowledge
from faultline.topology.compose import service_nodes, static_edges

ROOT = Path(__file__).resolve().parent.parent
DITTO_KNOWLEDGE = ROOT / "config" / "ditto_knowledge.yaml"


# ---------------------------------------------------------------- fixtures / helpers
COMPOSE_YAML = """\
services:
  mongodb:
    image: mongo:7.0
    volumes: ["app_data:/data/db"]
  api:
    build: {context: ../..}
    depends_on: [mongodb]
    environment:
      - MONGO_DB_HOSTNAME=mongodb
      - MONGO_DB_PASSWORD=hunter2
    healthcheck: {test: "curl --fail http://localhost:7626/alive || exit 1"}
    networks: {app: {aliases: [api-cluster]}}
  worker:
    image: acme/worker:1.2
    environment: {API_URL: "http://api:8080"}
    healthcheck: {test: ["CMD", "curl", "http://127.0.0.1:7626/ready"]}
    networks: {app: {aliases: [api-cluster]}}
  frontend:
    build: ./ui
    ports: ["3000:80"]
  grafana:
    image: grafana/grafana:11.1.0
  nginx:
    image: nginx:1.27
    ports: ["8080:80"]
    depends_on: [frontend]
volumes: {app_data: {}}
networks: {app: {}}
"""

NGINX_CONF = """
upstream backend { server api:8080; }
server {
  listen 80;
  location /api { proxy_pass http://api:8080; }
  location /    { proxy_pass http://frontend:80/; }
}
"""

README_MD = """\
---
title: Acme Shop
layout: default
---
# Acme Shop

{% include nav.html %}

<!-- internal note: do not ship this sentence -->

![architecture diagram](img/arch.png)

Read the [architecture guide](https://docs.example.test/arch) before touching the api.

```python
print("secret code block")
```

The api stores orders in mongodb and the worker drains the queue.
"""

FILLER = "The gateway fans out to the downstream services over the cluster. "


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _doc(path: Path, marker: str, repeat: int = 8) -> Path:
    return _write(path, f"# {marker}\n\n" + (FILLER * repeat))


class StubLLM:
    """Stands in for faultline.planning.llm.LLMClient. Never touches the network."""

    def __init__(self, payload=None, ok: bool = True, error: str = "", name: str = "stub:model-1"):
        self.payload = payload
        self._ok = ok
        self.last_error = error
        self._name = name
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._ok

    def describe(self) -> str:
        return self._name

    def chat_json(self, system: str, user: str, max_tokens: int = 700):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        return self.payload


def _silent(_msg: str) -> None:
    pass


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A repo with a toy root compose, the real one under deployment/, a README and arch docs."""
    root = tmp_path / "Acme_Shop"
    _write(root / "docker-compose.yml", "services: {api: {image: acme/api:dev}}\n")
    _write(root / "deployment" / "docker" / "docker-compose.yml", COMPOSE_YAML)
    _write(root / "deployment" / "docker" / "nginx.conf", NGINX_CONF)
    _write(root / "deployment" / "docker" / "README.md", "# Running the stack\n\n" + FILLER * 4)
    _write(root / "README.md", README_MD)
    _doc(root / "docs" / "architecture-overview.md", "Architecture overview")
    _doc(root / "docs" / "architecture-services-gateway.md", "Gateway service")
    _doc(root / "docs" / "overview.md", "Product overview")
    _doc(root / "docs" / "adr" / "0007-architecture-decision-record.md", "ADR")
    _doc(root / "docs" / "architecture-changelog.md", "Changelog")
    _doc(root / "docs" / "contributing-architecture.md", "Contributing")
    _write(root / "docs" / "architecture-tiny.md", "# too small\n")
    _write(root / "docs" / "architecture-huge.md", "# huge\n" + "z" * 130_000)
    return root


@pytest.fixture
def src(fake_repo: Path, tmp_path: Path) -> S.RepoSource:
    return S.resolve(str(fake_repo), tmp_path / "cache", log=_silent)


@pytest.fixture
def ex(fake_repo: Path) -> A.Extraction:
    d = fake_repo / "deployment" / "docker"
    return A.extract(d / "docker-compose.yml", d / "nginx.conf")


def _ex_from(compose: dict, nginx: str | None = None) -> A.Extraction:
    nodes = service_nodes(compose, None)
    return A.Extraction(nodes=nodes, edges=static_edges(compose, nodes, nginx), compose=compose)


# ================================================================ source.parse_reference
def test_parse_reference_shorthand_becomes_github_url():
    assert S.parse_reference("eclipse-ditto/ditto") == ("git", "https://github.com/eclipse-ditto/ditto.git")


@pytest.mark.parametrize("ref", [
    "https://github.com/eclipse-ditto/ditto",
    "https://github.com/eclipse-ditto/ditto.git",
    "http://github.com/eclipse-ditto/ditto/",
    "www.github.com/eclipse-ditto/ditto",
])
def test_parse_reference_normalises_full_github_urls(ref):
    assert S.parse_reference(ref) == ("git", "https://github.com/eclipse-ditto/ditto.git")


def test_parse_reference_existing_local_path_is_absolute(fake_repo: Path, monkeypatch):
    monkeypatch.chdir(fake_repo.parent)
    kind, value = S.parse_reference(fake_repo.name)
    assert kind == "local"
    assert Path(value).is_absolute() and Path(value) == fake_repo.resolve()


def test_parse_reference_rejects_nonexistent_nonrepo(tmp_path: Path):
    with pytest.raises(S.SourceError):
        S.parse_reference(str(tmp_path / "nope" / "missing dir"))
    with pytest.raises(S.SourceError):
        S.parse_reference("this is not a repo")


def test_parse_reference_rejects_empty_string():
    for ref in ("", "   "):
        with pytest.raises(S.SourceError, match="empty"):
            S.parse_reference(ref)


# ================================================================ source.slugify
@pytest.mark.parametrize("raw,expected", [
    ("Eclipse_Ditto", "eclipse-ditto"),
    ("Acme.Shop.v2", "acme-shop-v2"),
    ("  Ditto--Thing!!  ", "ditto-thing"),
    ("###", "repo"),
    ("", "repo"),
])
def test_slugify(raw, expected):
    assert S.slugify(raw) == expected


# ================================================================ source.find_compose
def test_find_compose_prefers_deployment_over_root(fake_repo: Path):
    found = S.find_compose(fake_repo)
    assert found.relative_to(fake_repo).as_posix() == "deployment/docker/docker-compose.yml"


def test_find_compose_falls_back_to_bounded_rglob_shallowest_first(tmp_path: Path):
    _write(tmp_path / "infra" / "stack" / "docker-compose.prod.yml", "services: {}\n")
    shallow = _write(tmp_path / "ops" / "compose.yaml", "services: {}\n")
    _write(tmp_path / "a" / "b" / "c" / "d" / "e" / "docker-compose.yml", "services: {}\n")  # too deep
    assert S.find_compose(tmp_path) == shallow


def test_find_compose_ignores_skip_dirs(tmp_path: Path):
    for skipped in ("node_modules/pkg", "tests", ".git"):
        _write(tmp_path / skipped / "docker-compose.yml", "services: {}\n")
    real = _write(tmp_path / "ops" / "compose.yml", "services: {}\n")
    assert S.find_compose(tmp_path) == real


def test_find_compose_raises_when_only_skipped_dirs_match(tmp_path: Path):
    _write(tmp_path / "node_modules" / "x" / "docker-compose.yml", "services: {}\n")
    _write(tmp_path / "tests" / "docker-compose.yml", "services: {}\n")
    with pytest.raises(S.SourceError, match="no docker-compose file"):
        S.find_compose(tmp_path)


def test_find_compose_raises_when_absent(tmp_path: Path):
    _write(tmp_path / "README.md", "nothing to see\n")
    with pytest.raises(S.SourceError, match="no docker-compose file"):
        S.find_compose(tmp_path)


def test_find_compose_honours_explicit_argument(fake_repo: Path):
    rel = S.find_compose(fake_repo, explicit="docker-compose.yml")
    assert rel == fake_repo / "docker-compose.yml"
    absolute = fake_repo / "deployment" / "docker" / "docker-compose.yml"
    assert S.find_compose(fake_repo, explicit=str(absolute)) == absolute


def test_find_compose_explicit_missing_path_raises(fake_repo: Path):
    with pytest.raises(S.SourceError, match="does not exist"):
        S.find_compose(fake_repo, explicit="deployment/nope.yml")


# ================================================================ source.find_docs
def test_find_docs_scores_overview_above_service_page_above_generic(fake_repo: Path):
    names = [p.name for p in S.find_docs(fake_repo)]
    assert names[:3] == ["architecture-overview.md", "architecture-services-gateway.md", "overview.md"]


def test_find_docs_excludes_penalised_and_out_of_size_files(fake_repo: Path):
    names = {p.name for p in S.find_docs(fake_repo, limit=20)}
    assert names == {"architecture-overview.md", "architecture-services-gateway.md", "overview.md"}
    for excluded in ("0007-architecture-decision-record.md", "architecture-changelog.md",
                     "contributing-architecture.md", "architecture-tiny.md", "architecture-huge.md"):
        assert excluded not in names


def test_find_docs_respects_limit(fake_repo: Path):
    assert [p.name for p in S.find_docs(fake_repo, limit=1)] == ["architecture-overview.md"]
    assert len(S.find_docs(fake_repo, limit=2)) == 2


def test_find_docs_empty_when_nothing_matches(tmp_path: Path):
    _doc(tmp_path / "docs" / "faq.md", "FAQ")
    assert S.find_docs(tmp_path) == []


# ================================================================ source.find_readme / find_nginx_conf
def test_find_readme_prefers_root_over_deployment_local(fake_repo: Path):
    compose = fake_repo / "deployment" / "docker" / "docker-compose.yml"
    assert S.find_readme(fake_repo, compose) == fake_repo / "README.md"


def test_find_readme_falls_back_to_compose_local_copy(tmp_path: Path):
    compose = _write(tmp_path / "deploy" / "docker-compose.yml", "services: {}\n")
    local = _write(tmp_path / "deploy" / "README.md", "# local\n")
    assert S.find_readme(tmp_path, compose) == local


def test_find_readme_absent(tmp_path: Path):
    compose = _write(tmp_path / "docker-compose.yml", "services: {}\n")
    assert S.find_readme(tmp_path, compose) is None


def test_find_nginx_conf_present_and_absent(tmp_path: Path):
    assert S.find_nginx_conf(tmp_path) is None
    conf = _write(tmp_path / "nginx.conf", NGINX_CONF)
    assert S.find_nginx_conf(tmp_path) == conf
    nested = tmp_path / "other"
    nested_conf = _write(nested / "conf" / "nginx.conf", NGINX_CONF)
    assert S.find_nginx_conf(nested) == nested_conf


# ================================================================ source.resolve
def test_resolve_local_repo_populates_everything(src: S.RepoSource, fake_repo: Path):
    assert src.slug == "acme-shop"
    assert src.root == fake_repo
    assert src.compose_path == fake_repo / "deployment" / "docker" / "docker-compose.yml"
    assert src.readme_path == fake_repo / "README.md"
    assert src.nginx_conf_path == fake_repo / "deployment" / "docker" / "nginx.conf"
    assert [p.name for p in src.doc_paths][0] == "architecture-overview.md"
    assert src.notes == []


def test_resolve_notes_missing_readme_and_nginx(tmp_path: Path):
    root = tmp_path / "bare-app"
    _write(root / "docker-compose.yml", COMPOSE_YAML)
    out = S.resolve(str(root), tmp_path / "cache", log=_silent)
    assert out.readme_path is None and out.nginx_conf_path is None and out.doc_paths == []
    joined = " | ".join(out.notes)
    assert "no README found" in joined and "no nginx config" in joined


def test_resolve_rejects_file_instead_of_directory(tmp_path: Path):
    f = _write(tmp_path / "plain.txt", "x\n")
    with pytest.raises(S.SourceError):
        S.resolve(str(f), tmp_path / "cache", log=_silent)


# ================================================================ agent.extract
def test_extract_finds_services_and_structural_edges(ex: A.Extraction):
    assert ex.names == ["api", "frontend", "grafana", "mongodb", "nginx", "worker"]
    kinds = {(e.src, e.dst, e.kind) for e in ex.edges}
    assert ("api", "mongodb", "depends_on") in kinds          # depends_on
    assert ("api", "mongodb", "env") in kinds                 # MONGO_DB_HOSTNAME
    assert ("worker", "api", "env") in kinds                  # API_URL
    assert ("nginx", "api", "proxy") in kinds                 # nginx proxy_pass
    assert ("nginx", "frontend", "proxy") in kinds             # second proxy_pass location
    assert ("nginx", "frontend", "depends_on") in kinds
    assert len(ex.edges) == 6
    assert not any(e.src == e.dst for e in ex.edges)
    # secrets are never turned into edges or evidence
    assert all("hunter2" not in e.evidence for e in ex.edges)


def test_extract_without_nginx_drops_proxy_edges(fake_repo: Path):
    compose = fake_repo / "deployment" / "docker" / "docker-compose.yml"
    assert not any(e.kind == "proxy" for e in A.extract(compose, None).edges)


def test_service_facts_shape(ex: A.Extraction):
    facts = {f["name"]: f for f in A.service_facts(ex)}
    assert facts["api"]["image"] == "(built from source)"
    assert facts["api"]["healthcheck_port"] == 7626
    assert facts["api"]["network_aliases"] == ["api-cluster"]
    assert facts["mongodb"]["stateful"] is True
    assert "MONGO_DB_HOSTNAME" in facts["api"]["env_keys"]


# ================================================================ agent.infer_health_port
def test_infer_health_port_shared_by_two_services(ex: A.Extraction):
    assert A.infer_health_port(ex) == 7626


def test_infer_health_port_none_when_single_or_absent():
    one = {"services": {"a": {"image": "a", "healthcheck": {"test": "curl http://a:7626/alive"}},
                        "b": {"image": "b"}}}
    assert A.infer_health_port(_ex_from(one)) is None
    assert A.infer_health_port(_ex_from({"services": {"a": {"image": "a"}}})) is None


def test_infer_health_port_none_when_two_ports_disagree():
    compose = {"services": {
        "a": {"image": "a", "healthcheck": {"test": "curl http://a:7626/alive"}},
        "b": {"image": "b", "healthcheck": {"test": "curl http://b:9090/alive"}},
    }}
    assert A.infer_health_port(_ex_from(compose)) is None


def test_infer_health_port_picks_the_modal_port():
    compose = {"services": {
        "a": {"image": "a", "healthcheck": {"test": "curl http://a:7626/alive"}},
        "b": {"image": "b", "healthcheck": {"test": "curl http://b:7626/alive"}},
        "c": {"image": "c", "healthcheck": {"test": "curl http://c:9090/alive"}},
    }}
    assert A.infer_health_port(_ex_from(compose)) == 7626


# ================================================================ agent markdown cleaning
def test_clean_markdown_strips_noise_and_keeps_link_text():
    out = A._clean_markdown(README_MD)
    assert "title: Acme Shop" not in out and "layout: default" not in out   # front-matter
    assert "{%" not in out and "nav.html" not in out                        # liquid tags
    assert "do not ship this sentence" not in out and "<!--" not in out     # html comments
    assert "img/arch.png" not in out                                        # image markdown
    assert "[architecture guide]" in out                                    # link text kept
    assert "https://docs.example.test/arch" not in out                      # link target dropped
    assert "[code block omitted]" in out and "secret code block" not in out
    assert "\n\n\n" not in out


def test_readme_excerpt_missing_file_returns_empty(tmp_path: Path):
    assert A.readme_excerpt(None) == ""
    assert A.readme_excerpt(tmp_path / "nope.md") == ""
    assert A.readme_excerpt(tmp_path) == ""  # a directory is not a file


def test_readme_excerpt_short_file_is_returned_whole(fake_repo: Path):
    out = A.readme_excerpt(fake_repo / "README.md")
    assert out.startswith("# Acme Shop")
    assert out == A._clean_markdown(README_MD)


def test_readme_excerpt_prefers_architecture_sections_within_budget(tmp_path: Path):
    def section(title: str, marker: str) -> str:
        return f"## {title}\n\n" + f"{marker} " * 300 + "\n\n"

    body = ("Acme Shop is a demo shop.\n\n"
            + section("Architecture", "ARCHMARKER")
            + section("Components", "COMPMARKER")
            + section("License", "LICMARKER")
            + section("Contributing", "CONTRIBMARKER")
            + section("Star History", "STARMARKER")
            + section("Installation on Windows", "INSTALLMARKER"))
    path = _write(tmp_path / "README.md", body)
    out = A.readme_excerpt(path)
    assert len(out) <= A.MAX_README_CHARS
    assert "Acme Shop is a demo shop." in out          # head is always kept
    assert "ARCHMARKER" in out and "COMPMARKER" in out
    assert "LICMARKER" not in out and "STARMARKER" not in out
    assert out.index("ARCHMARKER") < out.index("COMPMARKER")  # original document order
    assert len(A.readme_excerpt(path, limit=400)) <= 400


def test_docs_excerpt_labels_each_file_relative_to_root(fake_repo: Path):
    docs = S.find_docs(fake_repo, limit=2)
    out = A.docs_excerpt(docs, fake_repo)
    assert "--- docs/architecture-overview.md ---" in out
    assert "--- docs/architecture-services-gateway.md ---" in out
    assert str(fake_repo) not in out  # never an absolute path


def test_docs_excerpt_caps_a_single_file_at_max_doc_chars(tmp_path: Path):
    big = _write(tmp_path / "docs" / "architecture.md", "A" * (A.MAX_DOC_CHARS + 4000))
    out = A.docs_excerpt([big], tmp_path)
    body = out.split("---\n", 1)[1]
    assert len(body) == A.MAX_DOC_CHARS


def test_docs_excerpt_stops_at_the_budget(tmp_path: Path):
    first = _write(tmp_path / "a.md", "A" * 4000)
    second = _write(tmp_path / "b.md", "B" * 4000)
    third = _write(tmp_path / "c.md", "C" * 4000)
    out = A.docs_excerpt([first, second, third], tmp_path, budget=5000)
    assert out.count("B") == 1000          # truncated to the remaining budget
    assert "--- c.md ---" not in out       # budget exhausted before the third file
    assert out.count("A") == 4000


def test_docs_excerpt_tolerates_unreadable_and_empty_files(tmp_path: Path):
    good = _write(tmp_path / "good.md", "real architecture notes")
    empty = _write(tmp_path / "empty.md", "")
    missing = tmp_path / "missing.md"
    out = A.docs_excerpt([missing, empty, good], tmp_path)
    assert "real architecture notes" in out
    assert "missing.md" not in out and "empty.md" not in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a 0o000 file")
@pytest.mark.xfail(strict=True, reason="BUG: readme_excerpt calls read_text() unguarded, so an "
                                       "unreadable README raises PermissionError (docs_excerpt "
                                       "catches OSError and skips the file)")
def test_readme_excerpt_tolerates_unreadable_file(tmp_path: Path):
    path = _write(tmp_path / "README.md", "# hi\n" + FILLER * 10)
    os.chmod(path, 0o000)
    try:
        assert A.readme_excerpt(path) == ""
    finally:
        os.chmod(path, 0o644)


# ================================================================ agent.build_user_prompt
def test_build_user_prompt_contains_services_known_edges_and_example(ex: A.Extraction):
    prompt = A.build_user_prompt(ex, "readme text", "docs text", "example: yaml", "acme-shop")
    assert "APPLICATION: acme-shop" in prompt
    for name in ex.names:
        assert name in prompt
    assert "api -> mongodb (depends_on)" in prompt
    assert "readme text" in prompt and "docs text" in prompt and "example: yaml" in prompt


# ================================================================ agent.validate  (code decides)
def test_validate_accepts_a_well_formed_edge(ex: A.Extraction):
    raw = {"documented_edges": [{"src": "frontend", "dst": "api",
                                 "evidence": "the frontend calls the api for every page"}]}
    k, rejected = A.validate(raw, ex)
    assert [(e.src, e.dst) for e in k.documented_edges] == [("frontend", "api")]
    assert k.documented_edges[0].evidence == "the frontend calls the api for every page"
    assert rejected == []


def test_validate_drops_edges_naming_unknown_services(ex: A.Extraction):
    raw = {"documented_edges": [
        {"src": "api", "dst": "redis", "evidence": "api caches sessions in redis"},
        {"src": "ghost", "dst": "api", "evidence": "ghost calls the api"},
        {"src": "", "dst": "", "evidence": "nothing at all"},
    ]}
    k, rejected = A.validate(raw, ex)
    assert k.documented_edges == []
    assert len(rejected) == 3
    assert all("unknown service" in r for r in rejected)
    assert any("redis" in r for r in rejected)


def test_validate_drops_self_edges_and_duplicates(ex: A.Extraction):
    raw = {"documented_edges": [
        {"src": "api", "dst": "api", "evidence": "the api talks to itself"},
        {"src": "worker", "dst": "grafana", "evidence": "worker pushes dashboards to grafana"},
        {"src": "worker", "dst": "grafana", "evidence": "a second, different reason entirely"},
    ]}
    k, rejected = A.validate(raw, ex)
    assert [(e.src, e.dst) for e in k.documented_edges] == [("worker", "grafana")]
    assert k.documented_edges[0].evidence.startswith("worker pushes")  # the first one wins
    assert any("self-edge" in r for r in rejected)


def test_validate_truncates_long_evidence_and_collapses_whitespace(ex: A.Extraction):
    raw = {"documented_edges": [{"src": "frontend", "dst": "api",
                                 "evidence": "because\n   the\tfrontend needs it " + "y" * 500}]}
    k, _ = A.validate(raw, ex)
    evidence = k.documented_edges[0].evidence
    assert len(evidence) == 240
    assert evidence.startswith("because the frontend needs it y")


def test_validate_tolerates_junk_in_the_edge_list(ex: A.Extraction):
    raw = {"documented_edges": ["a bare string", None, 42, [], {"src": "frontend", "dst": "api"}]}
    k, rejected = A.validate(raw, ex)
    assert len(k.documented_edges) == 1
    assert sum("is not an object" in r for r in rejected) == 4


def test_validate_restated_structural_edge_needs_a_new_reason(ex: A.Extraction):
    raw = {"documented_edges": [
        {"src": "api", "dst": "mongodb"},                                           # default evidence
        {"src": "worker", "dst": "api", "evidence": "the worker polls the api for new jobs"},
    ]}
    k, rejected = A.validate(raw, ex)
    assert [(e.src, e.dst) for e in k.documented_edges] == [("worker", "api")]
    assert any("duplicates a structural edge" in r for r in rejected)


def test_validate_drops_role_for_unknown_service_and_unknown_role(ex: A.Extraction):
    k, rejected = A.validate({"roles": {"ghost": "app", "worker": "database"}}, ex)
    assert k.roles == {}
    assert any("unknown service 'ghost'" in r for r in rejected)
    assert any("'database'" in r and "worker" in r for r in rejected)


def test_validate_drops_role_that_merely_agrees_with_the_guess(ex: A.Extraction):
    assert ex.nodes["mongodb"].role == "datastore" and ex.nodes["nginx"].role == "proxy"
    k, rejected = A.validate({"roles": {"mongodb": "datastore", "nginx": "PROXY"}}, ex)
    assert k.roles == {}
    assert rejected == []  # agreement is not an error, it is simply not an override


def test_validate_refuses_ui_or_other_for_a_service_built_from_source(ex: A.Extraction):
    """The guard that stops a product service being silently exempted from chaos testing."""
    for role in ("ui", "other"):
        k, rejected = A.validate({"roles": {"frontend": role}}, ex)
        assert k.roles == {}, f"{role!r} must not be accepted for a service with build:"
        assert any("built from source" in r and "frontend" in r for r in rejected)


def test_validate_allows_ui_or_other_for_an_image_only_service(ex: A.Extraction):
    assert "build" not in (ex.compose["services"]["grafana"] or {})
    assert A.validate({"roles": {"grafana": "other"}}, ex)[0].roles == {"grafana": "other"}
    assert A.validate({"roles": {"grafana": "ui"}}, ex)[0].roles == {"grafana": "ui"}


def test_validate_rejects_cluster_alias_that_is_not_declared(ex: A.Extraction):
    k, rejected = A.validate({"cluster_alias": "acme-cluster"}, ex)
    assert k.cluster_alias is None
    assert any("not a network alias" in r for r in rejected)


def test_validate_keeps_a_real_cluster_alias(ex: A.Extraction):
    k, rejected = A.validate({"cluster_alias": " api-cluster "}, ex)
    assert k.cluster_alias == "api-cluster"
    assert rejected == []


def test_validate_prefers_the_code_derived_health_port(ex: A.Extraction):
    k, rejected = A.validate({"health_port": 8080}, ex)
    assert k.health_port == 7626
    assert any("8080" in r and "7626" in r and "overridden" in r for r in rejected)


def test_validate_fills_in_the_derived_health_port_when_the_model_is_silent(ex: A.Extraction):
    k, rejected = A.validate({}, ex)
    assert k.health_port == 7626
    assert rejected == []  # silence is not an error


def test_validate_health_port_garbage_is_reported_without_raising():
    compose = {"services": {"a": {"image": "a"}, "b": {"image": "b"}}}
    nothing_derived = _ex_from(compose)
    k, rejected = A.validate({"health_port": "not-a-port"}, nothing_derived)
    assert k.health_port is None
    assert any("not an integer" in r for r in rejected)
    k, rejected = A.validate({"health_port": 99999}, nothing_derived)
    assert k.health_port is None
    assert any("out of range" in r for r in rejected)
    assert A.validate({"health_port": None}, nothing_derived)[0].health_port is None


def test_validate_keeps_only_http_references_and_caps_them(ex: A.Extraction):
    refs = ["https://example.test/arch", " http://example.test/design ", "ftp://example.test/x",
            "see the wiki", None] + [f"https://example.test/p{i}" for i in range(12)]
    k, _ = A.validate({"references": refs}, ex)
    assert len(k.references) == 8
    assert k.references[0] == "https://example.test/arch"
    assert k.references[1] == "http://example.test/design"
    assert all(r.startswith("http") for r in k.references)


def test_validate_caps_the_edge_list_at_max_edges():
    names = [f"s{i}" for i in range(10)]
    compose = {"services": {n: {"image": f"acme/{n}:1"} for n in names}}
    big = _ex_from(compose)
    raw = {"documented_edges": [
        {"src": a, "dst": b, "evidence": f"{a} needs {b} to serve requests"}
        for a in names for b in names if a != b
    ]}
    assert len(raw["documented_edges"]) == 90
    k, rejected = A.validate(raw, big)
    assert len(k.documented_edges) == A.MAX_EDGES
    assert any(f"kept the first {A.MAX_EDGES} of 90" in r for r in rejected)


def test_validate_always_returns_a_valid_repo_knowledge(ex: A.Extraction):
    raw = {"health_port": "7626", "cluster_alias": 12345, "roles": {"frontend": "ui", "x": "y"},
           "documented_edges": [None, {"src": "api", "dst": "nope"}, {"src": " worker ", "dst": "mongodb",
                                                                      "evidence": "worker persists results"}],
           "references": ["nope", "https://ok.test/a"]}
    k, rejected = A.validate(raw, ex)
    assert RepoKnowledge.model_validate(k.model_dump()) == k
    assert [(e.src, e.dst) for e in k.documented_edges] == [("worker", "mongodb")]
    assert rejected


def test_validate_of_an_empty_answer_is_an_empty_knowledge():
    nothing = _ex_from({"services": {"a": {"image": "a"}}})
    k, rejected = A.validate({}, nothing)
    assert k == RepoKnowledge()
    assert rejected == []


@pytest.mark.xfail(strict=True, reason="BUG: validate() calls .items() on raw['roles'] without a "
                                       "type check, so a model answering with a non-empty list or "
                                       "string for `roles` raises AttributeError instead of being "
                                       "rejected")
def test_validate_tolerates_roles_that_are_not_a_mapping(ex: A.Extraction):
    k, rejected = A.validate({"roles": ["api", "frontend"]}, ex)
    assert k.roles == {}
    assert rejected


# ================================================================ agent.render_yaml
def test_render_yaml_round_trips_and_omits_empty_keys(src: S.RepoSource):
    k = RepoKnowledge(health_port=7626, cluster_alias="api-cluster", roles={"grafana": "other"},
                      documented_edges=[{"src": "worker", "dst": "mongodb", "evidence": "worker writes results"}],
                      references=["https://example.test/arch"])
    text = A.render_yaml(k, src, "llm", "stub:model-1")
    data = yaml.safe_load(text)
    assert RepoKnowledge.model_validate(data) == k
    assert set(data) == {"health_port", "cluster_alias", "roles", "documented_edges", "references"}


def test_render_yaml_header_records_provenance(src: S.RepoSource):
    text = A.render_yaml(RepoKnowledge(), src, "llm", "stub:model-1")
    head = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert len(head) == 4
    assert "acme-shop" in head[0]
    assert "deployment/docker/docker-compose.yml" in head[2]
    assert "using llm [stub:model-1]" in head[2]
    assert str(src.reference) in head[2]
    assert "Review before trusting it" in head[3]


def test_render_yaml_omits_rather_than_emitting_null_or_empty_lists(src: S.RepoSource):
    text = A.render_yaml(RepoKnowledge(health_port=7626), src, "structural-only", "")
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
    assert "null" not in body and "[]" not in body and "{}" not in body
    for key in ("cluster_alias", "roles", "documented_edges", "references"):
        assert key not in body
    assert yaml.safe_load(text) == {"health_port": 7626}


def test_render_yaml_of_empty_knowledge_is_still_valid_yaml(src: S.RepoSource):
    text = A.render_yaml(RepoKnowledge(), src, "structural-only", "")
    data = yaml.safe_load(text)
    assert data == {}
    assert RepoKnowledge.model_validate(data) == RepoKnowledge()
    assert "using structural-only." in text  # no trailing "[model]" when there is no model


def test_render_yaml_sorts_roles(src: S.RepoSource):
    k = RepoKnowledge(roles={"zeta": "other", "alpha": "other", "mid": "ui"})
    assert list(yaml.safe_load(A.render_yaml(k, src, "llm", "m"))["roles"]) == ["alpha", "mid", "zeta"]


# ================================================================ agent.onboard
GOOD_PAYLOAD = {
    "health_port": 7626,
    "cluster_alias": "api-cluster",
    "roles": {"grafana": "other"},
    "documented_edges": [
        {"src": "frontend", "dst": "api", "evidence": "the browser bundle calls the api for every page"},
        {"src": "api", "dst": "ghost", "evidence": "references a service that does not exist"},
    ],
    "references": ["https://docs.example.test/arch"],
}


def test_onboard_writes_the_knowledge_file_and_reports(src: S.RepoSource, tmp_path: Path):
    llm = StubLLM(GOOD_PAYLOAD)
    out_dir = tmp_path / "out"
    report = A.onboard(src, llm, DITTO_KNOWLEDGE, out_dir, log=_silent)

    assert isinstance(report, OnboardingReport)
    assert OnboardingReport.model_validate(report.model_dump()) == report
    assert report.slug == "acme-shop"
    assert report.generated_by == "llm" and report.model == "stub:model-1"
    assert report.services == ["api", "frontend", "grafana", "mongodb", "nginx", "worker"]
    assert report.structural_edges == 6
    assert report.compose_path == str(src.compose_path)
    assert report.readme_path == str(src.readme_path)
    assert report.nginx_conf_path == str(src.nginx_conf_path)

    assert [(e.src, e.dst) for e in report.knowledge.documented_edges] == [("frontend", "api")]
    assert report.knowledge.roles == {"grafana": "other"}
    assert report.knowledge.health_port == 7626 and report.knowledge.cluster_alias == "api-cluster"
    assert any("unknown service" in r for r in report.rejected)

    out_path = out_dir / "acme-shop_knowledge.yaml"
    assert report.output_path == str(out_path) and out_path.is_file()
    on_disk = yaml.safe_load(out_path.read_text())
    assert RepoKnowledge.model_validate(on_disk) == report.knowledge

    # exactly one bounded LLM call, carrying the example file and the real service names
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["max_tokens"] == 2600
    assert "ditto-cluster" in call["user"] and "worker" in call["user"]
    assert call["system"] == A.SYSTEM


def test_onboard_dry_run_writes_nothing(src: S.RepoSource, tmp_path: Path):
    out_dir = tmp_path / "out"
    report = A.onboard(src, StubLLM(GOOD_PAYLOAD), DITTO_KNOWLEDGE, out_dir, log=_silent, write=False)
    assert not out_dir.exists()
    assert not Path(report.output_path).exists()
    assert report.knowledge.documented_edges  # the work still happened, it just was not persisted


def test_onboard_without_a_model_is_structural_only(src: S.RepoSource, tmp_path: Path):
    llm = StubLLM(GOOD_PAYLOAD, ok=False, error="ollama is not running")
    report = A.onboard(src, llm, DITTO_KNOWLEDGE, tmp_path / "out", log=_silent)
    assert report.generated_by == "structural-only" and report.model == ""
    assert report.knowledge == RepoKnowledge()
    assert report.rejected == []
    assert any("model unavailable: ollama is not running" in n for n in report.notes)
    assert llm.calls == []
    assert yaml.safe_load((tmp_path / "out" / "acme-shop_knowledge.yaml").read_text()) == {}


def test_onboard_with_no_llm_object_at_all(src: S.RepoSource, tmp_path: Path):
    report = A.onboard(src, None, DITTO_KNOWLEDGE, tmp_path / "out", log=_silent)
    assert report.generated_by == "structural-only"
    assert any("no client" in n for n in report.notes)


def test_onboard_with_unparseable_model_answer_is_structural_only(src: S.RepoSource, tmp_path: Path):
    llm = StubLLM(None, error="invalid JSON in response")
    report = A.onboard(src, llm, DITTO_KNOWLEDGE, tmp_path / "out", log=_silent)
    assert report.generated_by == "structural-only"
    assert report.knowledge == RepoKnowledge()
    assert any("no usable JSON" in n and "invalid JSON in response" in n for n in report.notes)
    assert len(llm.calls) == 1


def test_onboard_with_entirely_invalid_model_answer_still_writes_valid_output(src: S.RepoSource, tmp_path: Path):
    payload = {
        "health_port": "whatever",
        "cluster_alias": "made-up-cluster",
        "roles": {"frontend": "ui", "nope": "app", "worker": "nonsense"},
        "documented_edges": [{"src": "api", "dst": "cassandra", "evidence": "invented"},
                             {"src": "api", "dst": "api", "evidence": "self"}, "junk", None],
        "references": ["not a url"],
    }
    out_dir = tmp_path / "out"
    report = A.onboard(src, StubLLM(payload), DITTO_KNOWLEDGE, out_dir, log=_silent)
    k = report.knowledge
    assert report.generated_by == "llm"
    assert k.documented_edges == [] and k.roles == {} and k.references == [] and k.cluster_alias is None
    assert k.health_port == 7626          # derived from the compose healthchecks, not from the model
    assert len(report.rejected) >= 6
    data = yaml.safe_load((out_dir / "acme-shop_knowledge.yaml").read_text())
    assert RepoKnowledge.model_validate(data) == k


def test_onboard_notes_when_no_architecture_docs_exist(tmp_path: Path):
    root = tmp_path / "bare-app"
    _write(root / "docker-compose.yml", COMPOSE_YAML)
    bare = S.resolve(str(root), tmp_path / "cache", log=_silent)
    report = A.onboard(bare, StubLLM({"documented_edges": []}), DITTO_KNOWLEDGE, tmp_path / "out", log=_silent)
    assert any("no architecture documentation" in n for n in report.notes)


def test_onboard_without_an_example_file_still_works(src: S.RepoSource, tmp_path: Path):
    llm = StubLLM({"documented_edges": []})
    report = A.onboard(src, llm, tmp_path / "missing_example.yaml", tmp_path / "out", log=_silent)
    assert report.generated_by == "llm"
    assert "WORKED EXAMPLE" in llm.calls[0]["user"]


# ================================================================ agent.compare
TRUTH_YAML = """\
health_port: 7626
cluster_alias: api-cluster
roles:
  mongodb: datastore
  nginx: proxy
documented_edges:
  - {src: worker, dst: mongodb, evidence: "the worker persists job results"}
  - {src: api, dst: mongodb, evidence: "the api stores orders"}
  - {src: frontend, dst: api, evidence: "the storefront calls the api"}
"""


@pytest.fixture
def truth_path(tmp_path: Path) -> Path:
    return _write(tmp_path / "truth_knowledge.yaml", TRUTH_YAML)


def test_compare_precision_recall_f1_on_a_known_overlap(truth_path: Path, ex: A.Extraction):
    generated = RepoKnowledge(documented_edges=[
        {"src": "worker", "dst": "mongodb", "evidence": "hit"},
        {"src": "grafana", "dst": "api", "evidence": "extra"},
    ])
    edges = A.compare(generated, truth_path, ex)["edges"]
    assert edges == {
        "generated": 2, "truth": 3, "matched": 1,
        "precision": 0.5, "recall": 0.333, "f1": 0.4,
        "matched_pairs": ["worker -> mongodb"],
        "missed": ["api -> mongodb", "frontend -> api"],
        "extra": ["grafana -> api"],
    }


def test_compare_final_graph_coverage_counts_structural_edges(truth_path: Path, ex: A.Extraction):
    generated = RepoKnowledge(documented_edges=[{"src": "worker", "dst": "mongodb", "evidence": "hit"}])
    cov = A.compare(generated, truth_path, ex)["final_graph_coverage"]
    # api -> mongodb is a *structural* edge (depends_on + env), so the human's edge is not a miss
    assert cov == {"truth_relationships": 3, "covered_by_generated_or_structural": 2,
                   "coverage": 0.667, "uncovered": ["frontend -> api"]}
    assert "api -> mongodb" in A.compare(generated, truth_path, ex)["edges"]["missed"]


def test_compare_effective_roles_score_one_when_the_guesser_already_agrees(truth_path: Path, ex: A.Extraction):
    roles = A.compare(RepoKnowledge(), truth_path, ex)["roles"]
    assert roles["compared"] == len(ex.nodes)
    assert roles["accuracy"] == 1.0 and roles["disagreed"] == []
    assert roles["overrides_generated"] == {} and roles["overrides_truth"] == {"mongodb": "datastore",
                                                                              "nginx": "proxy"}


def test_compare_effective_roles_flag_a_disagreement(truth_path: Path, ex: A.Extraction):
    generated = RepoKnowledge(roles={"nginx": "app"})
    roles = A.compare(generated, truth_path, ex)["roles"]
    assert roles["accuracy"] == round((len(ex.nodes) - 1) / len(ex.nodes), 3)
    assert roles["disagreed"] == ["nginx: got app, truth proxy"]


def test_compare_scalar_fields_and_empty_generated(truth_path: Path, ex: A.Extraction):
    result = A.compare(RepoKnowledge(health_port=7626), truth_path, ex)
    assert result["health_port"] == {"generated": 7626, "truth": 7626, "match": True}
    assert result["cluster_alias"] == {"generated": None, "truth": "api-cluster", "match": False}
    assert result["edges"]["precision"] == 0.0 and result["edges"]["f1"] == 0.0


def test_compare_without_an_extraction_compares_raw_overrides(truth_path: Path):
    generated = RepoKnowledge(roles={"mongodb": "datastore"},
                              documented_edges=[{"src": "frontend", "dst": "api", "evidence": "x"}])
    result = A.compare(generated, truth_path, None)
    assert result["roles"] == {"compared": 1, "agreed": 1, "accuracy": 1.0,
                               "overrides_generated": {"mongodb": "datastore"},
                               "overrides_truth": {"mongodb": "datastore", "nginx": "proxy"},
                               "disagreed": []}
    # with no structural edges to fall back on, coverage is only what the model produced
    assert result["final_graph_coverage"]["coverage"] == 0.333


def test_compare_against_an_empty_truth_file_does_not_divide_by_zero(tmp_path: Path, ex: A.Extraction):
    empty = _write(tmp_path / "empty_knowledge.yaml", "")
    result = A.compare(RepoKnowledge(), empty, ex)
    assert result["edges"]["precision"] == 0.0 and result["edges"]["recall"] == 0.0
    assert result["final_graph_coverage"]["coverage"] == 0.0


# ================================================================ the hand-authored contract
def test_shipped_ditto_knowledge_file_validates_against_repo_knowledge():
    """The contract between the hand-written knowledge file and every generated one."""
    data = yaml.safe_load(DITTO_KNOWLEDGE.read_text())
    k = RepoKnowledge.model_validate(data)
    assert k.health_port == 7626 and k.cluster_alias == "ditto-cluster"
    assert k.roles["mongodb"] == "datastore" and k.roles["nginx"] == "proxy"
    assert all(e.src and e.dst and e.evidence for e in k.documented_edges)
    assert all(r.startswith("http") for r in k.references)
