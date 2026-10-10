"""Repo Onboarding Agent: turn an unseen compose app into a knowledge file.

The structural parsers in `faultline.topology.compose` already see `depends_on`, env-var hostnames
and nginx `proxy_pass`. What they cannot see is the relationships a project only states in prose:
cluster membership, "A asks B to authorise every request", "the search index is fed by events".
For Ditto a human wrote those down in `config/ditto_knowledge.yaml`. This agent does that job for
any repository, in one bounded LLM pass, and validates the result before anything hits disk.

Code decides what is admissible (which services exist, which roles are legal, which edges survive);
the model only proposes. Same contract as the rest of Faultline.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from faultline.schemas import ROLES, DocumentedEdge, Edge, OnboardingReport, RepoKnowledge, ServiceNode
from faultline.onboarding.source import RepoSource
from faultline.topology.compose import load_compose, parse_env, service_nodes, static_edges

MAX_README_CHARS = 5000
MAX_DOCS_CHARS = 16000
MAX_DOC_CHARS = 5000
MAX_PROMPT_CHARS = 42000   # providers cap request size; stay well inside it
MIN_DOCS_CHARS = 1500
MIN_PROMPT_CHARS = 6000    # floor for the shrink-and-retry: below this there is nothing to ask
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
MAX_EDGES = 80
MAX_KNOWN_EDGES = 200      # listing every edge of a 50-service stack is noise, not signal
FACTS_FLOOR = 1200
# A provider refusing a request for size says so in several different dialects.
SIZE_ERROR_RE = re.compile(r"413|too large|too long|too many tokens|context[ _]length|maximum context"
                           r"|rate[ _]?limit|tokens per minute|reduce your message", re.I)
# Things that are not the product even when this repository builds them from source: doc viewers,
# load generators, observability, mail catchers. A name/image signal in the same spirit as the role
# guessing in topology.compose -- never a per-repository exception.
TOOLING_RE = re.compile(
    r"\b(swagger|redoc|openapi|api-?docs?|docs?|load-?gen\w*|loadtest|k6|locust|jmeter|gatling"
    r"|prometheus|grafana|cadvisor|alertmanager|\w*exporter|jaeger|zipkin|tempo|loki|kibana"
    r"|dashboard|adminer|pgadmin|mailhog|mailpit|maildev|smtp4dev|tracetest)\b", re.I)
# A reverse proxy routes to whatever sits behind it, so "the proxy depends on X" says nothing about
# whether X is the product or a dashboard.
PROXYISH_RE = re.compile(r"\b(nginx|traefik|haproxy|envoy|caddy|proxy|ingress)\b", re.I)
PROPER_NOUN_RE = re.compile(r"\b([A-Z][a-z]{3,})\b")
# The system prompt forbids speculation presented as fact. When a model hedges in the evidence
# string it is telling us the edge is a guess, and a guessed edge silently inflates the
# criticality of whatever it points at -- so code takes the hint and drops it.
SPECULATION_RE = re.compile(
    r"\b(imply|implies|implying|implied|presumably|probably|likely|perhaps|possibly|might"
    r"|appears? to|seems? to|assume[sd]?|assuming|speculat\w*|unclear|uncertain|could be"
    r"|i think|not sure|guessing)\b", re.I)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
URL_RE = re.compile(r"https?://\S+")
# words every URL has, which therefore say nothing about whose URL it is
URL_NOISE = {"http", "https", "html", "htm", "www", "com", "org", "io", "dev", "net", "github", "docs", "index"}
SECTION_RE = re.compile(r"^#{1,3}\s+(.+)$", re.M)
ARCH_WORDS = ("architect", "component", "service", "overview", "design", "concept", "cluster",
              "module", "how it works", "building block", "microservice", "deployment", "stack")
NOISE_WORDS = ("license", "contributing", "changelog", "sponsor", "badge", "installation on",
               "code of conduct", "acknowledg", "star history")

Log = Callable[[str], None]

SYSTEM = """You document the architecture of containerised applications for a chaos-engineering tool.

You are given one application's docker-compose services and its README. The tool can already derive
dependencies from `depends_on`, environment-variable hostnames and nginx `proxy_pass`. Your job is
ONLY to add what those cannot see: relationships the project states in prose or implies by design.

Rules:
- Use ONLY the exact service names given. Never invent a service.
- A documented edge means "src needs dst to work". Direction matters: put the dependent first.
- Do not restate an edge that is already listed as structurally known unless the prose gives a
  materially different reason for it.
- Every edge needs a short `evidence` string saying why, grounded in the README or in how the
  technology is known to work. No speculation presented as fact.
- `roles` only where the guessed role is wrong. Roles decide what gets broken, so be careful:
    "app"       a service that IS part of the product under test, including its own user-facing
                web front-ends. If this repository builds it, it is almost always "app".
    "datastore" a database, cache or broker the product stores state in.
    "proxy"     a reverse proxy or gateway in front of the product.
    "ui"/"other" EXCLUDED from chaos testing entirely. Use ONLY for things that are not the product:
                API-docs viewers (Swagger UI), monitoring and dashboards (Prometheus, Grafana,
                cAdvisor, exporters), and demo tooling. Never label a product service "ui" merely
                because it serves HTML -- that would silently exempt it from all testing.
- `health_port` only if the services expose a common internal health/management port that compose
  does not declare in a healthcheck. Otherwise null.
- `cluster_alias` only if services join one cluster through a shared network alias. Otherwise null.
- Prefer a few high-confidence edges over many speculative ones.

Answer with ONE JSON object and nothing else:
{"health_port": <int|null>, "cluster_alias": <string|null>,
 "roles": {"<service>": "datastore|app|proxy|ui|other"},
 "documented_edges": [{"src": "<service>", "dst": "<service>", "evidence": "<short reason>"}],
 "references": ["<url from the README that documents the architecture>"]}"""


# ---------------------------------------------------------------- extraction
@dataclass
class Extraction:
    nodes: dict[str, ServiceNode]
    edges: list[Edge]
    compose: dict

    @property
    def names(self) -> list[str]:
        return sorted(self.nodes)


def extract(compose_path: Path, nginx_conf_path: Path | None) -> Extraction:
    """Everything the generic parsers can already see, with no knowledge file applied."""
    compose = load_compose(compose_path)
    if not isinstance(compose.get("services"), dict):
        # `services` written as a list (or absent) makes the parsers raise AttributeError.
        # source.compose_services() rejects those files first, so this only guards direct callers.
        compose = {**compose, "services": {}}
    nodes = service_nodes(compose, knowledge=None)
    nginx_text = nginx_conf_path.read_text(errors="replace") if nginx_conf_path and nginx_conf_path.is_file() else None
    edges = static_edges(compose, nodes, nginx_text)
    return Extraction(nodes=nodes, edges=edges, compose=compose)


def service_facts(ex: Extraction, detail: int = 3) -> list[dict[str, Any]]:
    """One record per service. `detail` trades per-service richness for prompt size (3 = full).

    Every level keeps the name, the image and the guessed role, because those are what the model
    is asked to reason about and what its answer has to reference. What goes first is the bulk:
    commands, then published ports, then the environment-variable keys.
    """
    services = ex.compose.get("services") or {}
    out = []
    for name in ex.names:
        node = ex.nodes[name]
        svc = services.get(name) or {}
        env = parse_env(svc.get("environment"))
        fact: dict[str, Any] = {
            "name": name,
            "image": node.image or "(built from source)",
            "guessed_role": node.role,
        }
        if detail >= 2:
            fact["published_ports"] = [str(p) for p in (svc.get("ports") or [])][:4]
        fact["healthcheck_port"] = node.health_port
        fact["network_aliases"] = node.aliases
        fact["stateful"] = node.stateful
        if detail >= 1:
            fact["env_keys"] = sorted(env)[: (14 if detail >= 3 else 6)]
        if detail >= 3:
            cmd = " ".join(str(c) for c in svc["command"]) if isinstance(svc.get("command"), list) else svc.get("command")
            fact["command"] = (str(cmd)[:200] if cmd else None)
        out.append(fact)
    return out


def _clean_markdown(text: str) -> str:
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)      # Jekyll front-matter
    text = re.sub(r"\{%.*?%\}", "", text, flags=re.S)                # liquid tags
    text = re.sub(r"```.*?```", "[code block omitted]", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)                # images
    text = re.sub(r"\]\([^)]*\)", "]", text)                        # link targets: keep the text
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def docs_excerpt(paths: list[Path], root: Path, budget: int = MAX_DOCS_CHARS) -> str:
    """Concatenate the architecture docs, each labelled with its path, within a hard budget."""
    parts: list[str] = []
    for p in paths:
        if budget <= 0:
            break
        try:
            body = _clean_markdown(p.read_text(errors="replace"))
        except OSError:
            continue
        if not body:
            continue
        body = body[: min(MAX_DOC_CHARS, budget)]
        rel = p.relative_to(root).as_posix() if p.is_relative_to(root) else p.name
        parts.append(f"--- {rel} ---\n{body}")
        budget -= len(body)
    return "\n\n".join(parts)


def readme_excerpt(path: Path | None, limit: int = MAX_README_CHARS) -> str:
    """Bounded README text, biased toward the sections that describe the architecture."""
    if not path or not path.is_file():
        return ""
    try:
        raw = path.read_text(errors="replace")
    except OSError:
        return ""   # unreadable (permissions, dangling symlink): proceed without it, as docs_excerpt does
    text = _clean_markdown(raw)
    if len(text) <= limit:
        return text.strip()

    marks = [(m.start(), m.group(1).strip()) for m in SECTION_RE.finditer(text)]
    if not marks:
        return text[:limit].strip()
    bounds = [(s, marks[i + 1][0] if i + 1 < len(marks) else len(text), t) for i, (s, t) in enumerate(marks)]
    head = text[: marks[0][0]].strip()

    def score(title: str, body: str) -> int:
        t = title.lower()
        if any(w in t for w in NOISE_WORDS):
            return -5
        return sum(3 for w in ARCH_WORDS if w in t) + sum(1 for w in ARCH_WORDS if w in body.lower()[:1500])

    scored = [(score(t, text[a:b]), a, b) for a, b, t in bounds]
    # Never spend prompt budget on a penalised section (licence, contributing, changelog ...),
    # even when there is room: it crowds out architecture prose further down the document.
    ranked = [(a, b, None) for sc, a, b in sorted(scored, key=lambda x: -x[0]) if sc >= 0]
    chosen: list[tuple[int, int]] = []
    budget = limit - len(head)
    for start, end, _title in ranked:
        chunk = min(end - start, 4000)
        if budget - chunk < 0:
            continue
        chosen.append((start, start + chunk))
        budget -= chunk
    chosen.sort()
    parts = [head] + [text[a:b].strip() for a, b in chosen]
    return "\n\n".join(p for p in parts if p)[:limit].strip()


# ---------------------------------------------------------------- the single LLM pass
def _facts_block(ex: Extraction, budget: int) -> str:
    """The service table, compacted until it fits. Never drops a service: the model needs the names."""
    text = ""
    for detail in (3, 2, 1, 0):
        facts = service_facts(ex, detail=detail)
        text = json.dumps(facts, indent=1) if detail == 3 else json.dumps(facts, separators=(",", ":"))
        if len(text) <= budget:
            return text
    return text


def _known_block(ex: Extraction, budget: int) -> str:
    """The structural edges, newest-sorted order preserved, truncated with an honest count."""
    lines = [f"  {e.src} -> {e.dst} ({e.kind})" for e in ex.edges[:MAX_KNOWN_EDGES]]
    while lines and sum(len(ln) + 1 for ln in lines) > budget:
        lines.pop()
    if len(lines) < len(ex.edges):
        lines.append(f"  ... and {len(ex.edges) - len(lines)} further structural edges, omitted for size")
    return "\n".join(lines) or "  (none)"


def build_user_prompt(ex: Extraction, readme: str, docs: str, example_yaml: str, app_hint: str,
                      budget: int = MAX_PROMPT_CHARS) -> str:
    """Assemble the one request, inside a hard character budget.

    A 50-service monorepo produces more facts than any request is allowed to carry, and the failure
    mode is total: the provider rejects it and the knowledge file comes back empty. So everything
    that scales with repository size -- the service table and the structural edge listing -- is
    sized against the budget before the prose gets what is left. Priority order is service table,
    structural edges, documentation, README, because the model cannot invent the first two.
    """
    def assemble(facts: str, known: str, readme_t: str, docs_t: str) -> str:
        return "\n".join([
            f"APPLICATION: {app_hint}",
            "",
            "SERVICES (use exactly these names):",
            facts,
            "",
            f"ALREADY KNOWN STRUCTURALLY ({len(ex.edges)} edges; do not simply repeat these):",
            known,
            "",
            "README (may be truncated):",
            readme_t or "(no README available)",
            "",
            "ARCHITECTURE DOCUMENTATION shipped in the repository (may be truncated):",
            docs_t or "(none found)",
            "",
            "WORKED EXAMPLE — this is a DIFFERENT application (Eclipse Ditto). It shows the required",
            "shape and the level of detail expected. Do not copy its service names or its content:",
            example_yaml,
            "",
            f"Now produce the JSON object for {app_hint}.",
        ])

    room = max(FACTS_FLOOR * 2, budget - len(assemble("", "", "", "")))
    facts = _facts_block(ex, max(FACTS_FLOOR, int(room * 0.55)))
    known = _known_block(ex, max(400, int(room * 0.15)))
    prose = max(0, room - len(facts) - len(known))
    keep_readme = min(len(readme), prose if not docs else int(prose * 0.45))
    return assemble(facts, known, readme[:keep_readme], docs[: max(0, prose - keep_readme)])


def synthesize(ex: Extraction, readme: str, docs: str, example_yaml: str, app_hint: str, llm, log: Log = print):
    """One bounded pass, with shrink-and-retry if the provider rejects the payload size.

    What overflows a request is not always the prose: a 50-service stack with 300 structural edges
    overflows on facts alone, and halving an empty docs string does nothing. So a size rejection
    halves the whole prompt budget, which `build_user_prompt` then spends in priority order.
    """
    if llm is None or not llm.available():
        reason = (llm.last_error if llm else "no client") or "disabled"
        log(f"no model available ({reason}); writing a structural-only knowledge file")
        return None, f"model unavailable: {reason}"

    last_err = "unparseable"
    budget = MAX_PROMPT_CHARS
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # the provider caps the whole request, system prompt included
        prompt = build_user_prompt(ex, readme, docs, example_yaml, app_hint, budget=budget - len(SYSTEM))
        log(f"asking {llm.describe()} to document {len(ex.nodes)} services "
            f"({len(prompt) + len(SYSTEM)} chars) ...")
        data = llm.chat_json(SYSTEM, prompt, max_tokens=2600)
        if data is not None:
            return data, ""
        last_err = llm.last_error or "unparseable"
        if attempt == MAX_ATTEMPTS:
            break
        if SIZE_ERROR_RE.search(last_err) and budget > MIN_PROMPT_CHARS:
            budget = max(MIN_PROMPT_CHARS, budget // 2)
            docs, readme = docs[: max(MIN_DOCS_CHARS, len(docs) // 2)], readme[:3000]
            log(f"provider rejected the request size ({last_err[:70]}); "
                f"retrying inside a {budget}-char budget")
        else:
            # rate limits, timeouts and truncated JSON are all transient here; a config file
            # silently degrading to empty because of one bad response is worse than waiting.
            log(f"attempt {attempt} failed ({last_err[:70]}); retrying in {RETRY_BACKOFF_S * attempt:.0f}s")
            time.sleep(RETRY_BACKOFF_S * attempt)
    return None, f"model returned no usable JSON after {MAX_ATTEMPTS} attempts ({last_err[:160]})"


# ---------------------------------------------------------------- validation (code decides)
def infer_health_port(ex: Extraction) -> int | None:
    """A management port several services share is a fact, not a judgement call -- derive it."""
    ports = [n.health_port for n in ex.nodes.values() if n.health_port]
    if len(ports) < 2:
        return None
    common = max(set(ports), key=ports.count)
    return common if ports.count(common) >= 2 else None


def example_vocabulary(example_yaml: str) -> set[str]:
    """Identifiers that belong to the worked example and could not belong to anything else.

    The prompt carries `config/ditto_knowledge.yaml` so the model can see the required shape. A
    model short of material sometimes answers *about the example* instead. Its service names are
    already rejected for not existing in this repository, but an evidence string citing the
    example's technology ("over the Pekko cluster") would otherwise survive untouched.

    Only distinctive identifiers are collected -- compound service names and proper nouns -- never
    single generic words like "gateway" or "policies", which a different project may legitimately
    use. The caller subtracts whatever the target repository says about itself.
    """
    try:
        data = yaml.safe_load(example_yaml) or {}
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    vocab: set[str] = set()
    ids = [str(k) for k in (data.get("roles") or {})]
    ids += [str(e.get(k, "")) for e in (data.get("documented_edges") or []) if isinstance(e, dict) for k in ("src", "dst")]
    ids.append(str(data.get("cluster_alias") or ""))
    for ident in ids:
        if len(ident) > 3 and ("-" in ident or "_" in ident):
            vocab.add(ident.lower())
    prose = " ".join(str(e.get("evidence", "")) for e in (data.get("documented_edges") or []) if isinstance(e, dict))
    vocab |= {w.lower() for w in PROPER_NOUN_RE.findall(prose)}
    return vocab


def foreign_example_urls(example_yaml: str, own: set[str]) -> set[str]:
    """URLs the worked example cites that have nothing to do with this repository.

    A URL is this repository's own if any word in it -- a host label, a path segment -- is a word
    the repository uses about itself. That keeps Ditto's own links when Ditto is the target and
    drops them from everybody else's knowledge file.
    """
    return {u for u in URL_RE.findall(example_yaml)
            if not ({w.lower() for w in WORD_RE.findall(u)} - URL_NOISE) & own}


def own_vocabulary(ex: Extraction, *texts: str) -> set[str]:
    """Every word this repository uses about itself: service names, aliases, README, architecture docs."""
    words = {n.lower() for n in ex.nodes} | {a.lower() for n in ex.nodes.values() for a in n.aliases}
    words |= {n.image.lower() for n in ex.nodes.values() if n.image}
    for text in texts:
        words |= {w.lower() for w in WORD_RE.findall(text or "")}
    return words


def validate(raw: dict[str, Any], ex: Extraction, foreign_vocab: set[str] | None = None,
             foreign_urls: set[str] | None = None) -> tuple[RepoKnowledge, list[str]]:
    """Coerce a model answer into a RepoKnowledge that can only reference real services.

    `foreign_vocab` and `foreign_urls` are what the worked example says and this repository does
    not (see `example_vocabulary` / `foreign_example_urls`). An evidence string or a reference that
    can only have come from the example is contamination, not evidence, and does not survive.
    """
    rejected: list[str] = []
    names = set(ex.nodes)
    structural = {(e.src, e.dst) for e in ex.edges}
    foreign = {w for w in (foreign_vocab or set()) if w}

    compose_services = ex.compose.get("services") or {}
    # Who needs whom, for the exclusion guard below. Edges out of a reverse proxy do not count:
    # a proxy fronts whatever sits behind it and so says nothing about what that thing is.
    proxyish = {n for n, node in ex.nodes.items()
                if node.role == "proxy" or PROXYISH_RE.search(f"{n} {node.image}")}
    dependents: dict[str, set[str]] = {}
    for e in ex.edges:
        if e.src != e.dst:
            dependents.setdefault(e.dst, set()).add(e.src)

    roles: dict[str, str] = {}
    raw_roles = raw.get("roles")
    if raw_roles and not isinstance(raw_roles, dict):
        rejected.append(f"roles must be a mapping of service to role, got {type(raw_roles).__name__}")
        raw_roles = {}
    for svc, role in (raw_roles or {}).items():
        svc, role = str(svc).strip(), str(role).strip().lower()
        if svc not in names:
            rejected.append(f"role for unknown service {svc!r}")
            continue
        if role not in ROLES:
            rejected.append(f"role {role!r} for {svc} is not one of {ROLES}")
            continue
        if role == ex.nodes[svc].role:
            continue  # agrees with the structural guess; no override needed
        if role in ("ui", "other"):
            # `ui`/`other` set chaos_relevant=False, i.e. exempt the service from every experiment.
            # Two independent conditions have to hold before code accepts that.
            needed_by = sorted(dependents.get(svc, set()) - proxyish)
            if (compose_services.get(svc) or {}).get("build") and not TOOLING_RE.search(f"{svc} {ex.nodes[svc].image}"):
                # Built from this repository's own source and not recognisable as tooling, so it is
                # the product. Built-from-source tooling (a docs site, a load generator) passes.
                rejected.append(f"refused to mark {svc!r} as {role!r}: it is built from source in this repo")
                continue
            if needed_by:
                rejected.append(f"refused to mark {svc!r} as {role!r}: {', '.join(needed_by[:4])} "
                                f"depend{'s' if len(needed_by) == 1 else ''} on it, so it is in the blast radius")
                continue
        roles[svc] = role

    edges: list[DocumentedEdge] = []
    seen: set[tuple[str, str]] = set()
    for item in (raw.get("documented_edges") or []):
        if not isinstance(item, dict):
            rejected.append(f"edge {item!r} is not an object")
            continue
        src, dst = str(item.get("src", "")).strip(), str(item.get("dst", "")).strip()
        if src not in names or dst not in names:
            rejected.append(f"edge {src or '?'} -> {dst or '?'} references an unknown service")
            continue
        if src == dst:
            rejected.append(f"edge {src} -> {dst} is a self-edge")
            continue
        if (src, dst) in seen:
            continue
        seen.add((src, dst))
        evidence = " ".join(str(item.get("evidence") or "documented").split())[:240]
        if (src, dst) in structural and len(evidence) < 12:
            rejected.append(f"edge {src} -> {dst} duplicates a structural edge with no new reason")
            continue
        hedge = SPECULATION_RE.search(evidence)
        if hedge:
            rejected.append(f"edge {src} -> {dst} is speculative, not documented "
                            f"(evidence hedges: {hedge.group(0)!r})")
            continue
        borrowed = sorted(foreign & {w.lower() for w in WORD_RE.findall(evidence)})
        if borrowed:
            rejected.append(f"edge {src} -> {dst} cites the worked example "
                            f"({', '.join(borrowed[:3])}), not this repository")
            continue
        edges.append(DocumentedEdge(src=src, dst=dst, evidence=evidence))
    if len(edges) > MAX_EDGES:
        rejected.append(f"kept the first {MAX_EDGES} of {len(edges)} edges")
        edges = edges[:MAX_EDGES]

    port = raw.get("health_port")
    try:
        port = int(port) if port not in (None, "", "null") else None
    except (TypeError, ValueError):
        rejected.append(f"health_port {port!r} is not an integer")
        port = None
    if port is not None and not (1 <= port <= 65535):
        rejected.append(f"health_port {port} out of range")
        port = None
    derived = infer_health_port(ex)
    if derived and port != derived:
        if port is not None:
            rejected.append(f"health_port {port} overridden by {derived}, shared by the compose healthchecks")
        port = derived

    alias = raw.get("cluster_alias")
    alias = str(alias).strip() if alias not in (None, "", "null") else None
    if alias:
        declared = {a for n in ex.nodes.values() for a in n.aliases}
        if alias not in declared:
            rejected.append(f"cluster_alias {alias!r} is not a network alias in the compose file")
            alias = None

    refs: list[str] = []
    for item in (raw.get("references") or []):
        url = str(item).strip().rstrip(",;")
        if not url.startswith(("http://", "https://")):   # "httpfoo://evil" is not a URL
            continue
        if url in (foreign_urls or set()):
            # copied out of the worked example; it documents a different application
            rejected.append(f"reference {url} belongs to the worked example, not this repository")
            continue
        if url not in refs:
            refs.append(url)
    refs = refs[:8]

    return RepoKnowledge(health_port=port, cluster_alias=alias, roles=roles,
                         documented_edges=edges, references=refs), rejected


# ---------------------------------------------------------------- output
def render_yaml(k: RepoKnowledge, src: RepoSource, generated_by: str, model: str) -> str:
    rel = src.compose_path.relative_to(src.root) if src.compose_path.is_relative_to(src.root) else src.compose_path
    head = [
        f"# Documented knowledge about {src.slug} that cannot be derived from docker-compose alone.",
        "# The topology mapper layers these facts on top of what it parses from compose, env vars and nginx.",
        f"# Generated by `faultline onboard` from {src.reference} ({rel}) using {generated_by}"
        + (f" [{model}]" if model else "") + ".",
        "# Review before trusting it: documented edges change the criticality ranking.",
    ]
    body: dict[str, Any] = {}
    if k.health_port is not None:
        body["health_port"] = k.health_port
    if k.cluster_alias:
        body["cluster_alias"] = k.cluster_alias
    if k.roles:
        body["roles"] = dict(sorted(k.roles.items()))
    if k.documented_edges:
        body["documented_edges"] = [{"src": e.src, "dst": e.dst, "evidence": e.evidence} for e in k.documented_edges]
    if k.references:
        body["references"] = k.references
    text = yaml.safe_dump(body, sort_keys=False, default_flow_style=False, width=120, allow_unicode=True)
    return "\n".join(head) + "\n" + (text if body else "{}\n")


# ---------------------------------------------------------------- orchestration
def _is_substantive(path: Path) -> bool:
    try:
        existing = RepoKnowledge.model_validate(yaml.safe_load(path.read_text()) or {})
    except Exception:
        return False
    return bool(existing.documented_edges or existing.roles)


def onboard(src: RepoSource, llm, example_path: Path, out_dir: Path, log: Log = print,
            write: bool = True, force: bool = False) -> OnboardingReport:
    ex = extract(src.compose_path, src.nginx_conf_path)
    log(f"{src.slug}: {len(ex.nodes)} services, {len(ex.edges)} structural edges from "
        f"{src.compose_path.name}" + (f" + {src.nginx_conf_path.name}" if src.nginx_conf_path else ""))
    notes = list(src.notes)
    readme = readme_excerpt(src.readme_path)
    docs = docs_excerpt(src.doc_paths, src.root)
    if src.readme_path:
        log(f"README: {src.readme_path.name}, {len(readme)} chars after trimming")
    if src.doc_paths:
        log(f"architecture docs: {len(src.doc_paths)} file(s), {len(docs)} chars "
            f"({', '.join(p.name for p in src.doc_paths[:4])}{' ...' if len(src.doc_paths) > 4 else ''})")
    else:
        notes.append("no architecture documentation found; the model saw only the README")
    example_yaml = example_path.read_text() if example_path and example_path.is_file() else ""

    raw, err = synthesize(ex, readme, docs, example_yaml, src.slug, llm, log=log)
    rejected: list[str] = []
    if raw is None:
        knowledge, generated_by, model = RepoKnowledge(), "structural-only", ""
        notes.append(err)
    else:
        # Anything the worked example says that this repository never says about itself is
        # contamination from the prompt, not evidence about this application.
        own = own_vocabulary(ex, readme, docs, src.slug)
        knowledge, rejected = validate(raw, ex, foreign_vocab=example_vocabulary(example_yaml) - own,
                                       foreign_urls=foreign_example_urls(example_yaml, own))
        generated_by, model = "llm", llm.describe()
        log(f"model proposed {len(raw.get('documented_edges') or [])} edges, "
            f"{len(knowledge.documented_edges)} survived validation")
        for r in rejected:
            log(f"  rejected: {r}")

    out_path = out_dir / f"{src.slug}_knowledge.yaml"
    if write:
        degraded = generated_by == "structural-only" and not knowledge.documented_edges and not knowledge.roles
        if degraded and out_path.is_file() and _is_substantive(out_path) and not force:
            msg = (f"refusing to overwrite {out_path} with an empty file: the model failed and the "
                   f"existing file has content (pass --force to overwrite anyway)")
            log(msg)
            notes.append(msg)
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(render_yaml(knowledge, src, generated_by, model))
            log(f"wrote {out_path}")

    return OnboardingReport(
        slug=src.slug, source=src.reference, resolved_path=str(src.root),
        compose_path=str(src.compose_path), readme_path=str(src.readme_path) if src.readme_path else None,
        nginx_conf_path=str(src.nginx_conf_path) if src.nginx_conf_path else None,
        services=ex.names, structural_edges=len(ex.edges), generated_by=generated_by, model=model,
        knowledge=knowledge, rejected=rejected, notes=notes, output_path=str(out_path),
    )


# ---------------------------------------------------------------- acceptance comparison
def compare(generated: RepoKnowledge, truth_path: Path, ex: Extraction | None = None) -> dict[str, Any]:
    """Overlap against a hand-authored file. Ditto is the one case with ground truth.

    Roles are compared as *effective* roles (structural guess + override), because a knowledge file
    only needs to carry the overrides -- agreeing with the guesser by staying silent is correct.
    """
    truth = RepoKnowledge.model_validate(yaml.safe_load(Path(truth_path).read_text()) or {})
    g = {(e.src, e.dst) for e in generated.documented_edges}
    t = {(e.src, e.dst) for e in truth.documented_edges}
    hit, missed, extra = sorted(g & t), sorted(t - g), sorted(g - t)
    precision = len(hit) / len(g) if g else 0.0
    recall = len(hit) / len(t) if t else 0.0
    if ex is not None:
        base = {n: node.role for n, node in ex.nodes.items()}
        gr = {**base, **generated.roles}
        tr = {**base, **{k: v for k, v in truth.roles.items() if k in base}}
    else:
        gr, tr = generated.roles, truth.roles
    shared = sorted(set(gr) & set(tr))
    role_hit = sorted(k for k in shared if gr[k] == tr[k])
    # What actually matters: after merging with the structural edges, is every relationship the
    # human documented present in the graph? A documented edge the parsers already found is not a miss.
    structural = {(e.src, e.dst) for e in ex.edges} if ex is not None else set()
    union = g | structural
    covered = sorted(t & union)
    uncovered = sorted(t - union)
    return {
        "final_graph_coverage": {
            "truth_relationships": len(t), "covered_by_generated_or_structural": len(covered),
            "coverage": round(len(covered) / len(t), 3) if t else 0.0,
            "uncovered": [f"{a} -> {b}" for a, b in uncovered],
        },
        "edges": {"generated": len(g), "truth": len(t), "matched": len(hit),
                  "precision": round(precision, 3), "recall": round(recall, 3),
                  "f1": round(2 * precision * recall / (precision + recall), 3) if (precision + recall) else 0.0,
                  "matched_pairs": [f"{a} -> {b}" for a, b in hit],
                  "missed": [f"{a} -> {b}" for a, b in missed],
                  "extra": [f"{a} -> {b}" for a, b in extra]},
        "roles": {"compared": len(shared), "agreed": len(role_hit),
                  "accuracy": round(len(role_hit) / len(shared), 3) if shared else 0.0,
                  "overrides_generated": generated.roles, "overrides_truth": truth.roles,
                  "disagreed": sorted(f"{k}: got {gr[k]}, truth {tr[k]}" for k in shared if gr[k] != tr[k])},
        "health_port": {"generated": generated.health_port, "truth": truth.health_port,
                        "match": generated.health_port == truth.health_port},
        "cluster_alias": {"generated": generated.cluster_alias, "truth": truth.cluster_alias,
                          "match": generated.cluster_alias == truth.cluster_alias},
    }
