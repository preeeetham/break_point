import pytest

compose = pytest.importorskip("faultline.topology.compose")
graph = pytest.importorskip("faultline.topology.graph")
render = pytest.importorskip("faultline.topology.render")

from faultline.schemas import Topology  # noqa: E402

NGINX_CONF = """
upstream gw { server gateway:8080; }
server {
  listen 80;
  location /api { proxy_pass http://gateway:8080; }
  location / { proxy_pass http://ditto-ui:8080/; }
}
"""


@pytest.fixture
def small_compose() -> dict:
    env = ["MONGO_DB_HOSTNAME=mongodb", "MONGO_DB_PASSWORD=mongodb", "TZ=Europe/Berlin"]
    return {
        "services": {
            "mongodb": {"image": "mongo:7.0", "volumes": ["ditto_mongodb:/data/db"], "mem_limit": "1g"},
            "policies": {
                "image": "eclipse/ditto-policies:3.9.0",
                "environment": env,
                "healthcheck": {"test": "curl --fail `hostname`:7626/alive || exit 1", "interval": "30s"},
                "mem_limit": "512m",
            },
            "things": {"image": "eclipse/ditto-things:3.9.0", "environment": env, "depends_on": ["policies"]},
            "gateway": {"image": "eclipse/ditto-gateway:3.9.0", "environment": {"MONGO_DB_HOSTNAME": "mongodb"},
                        "depends_on": {"policies": {"condition": "service_started"}}},
            "nginx": {"image": "nginx:1.27", "ports": ["8080:80"], "depends_on": ["gateway"]},
        },
        "volumes": {"ditto_mongodb": {}},
    }


@pytest.fixture
def built(small_compose):
    nodes = compose.service_nodes(small_compose, {})
    edges = compose.static_edges(small_compose, nodes, NGINX_CONF)
    return nodes, edges


def test_service_nodes_roles_ports_limits(built):
    nodes, _ = built
    assert set(nodes) == {"mongodb", "policies", "things", "gateway", "nginx"}
    assert nodes["mongodb"].role == "datastore" and nodes["mongodb"].stateful is True
    assert nodes["nginx"].role == "proxy" and nodes["nginx"].entrypoint is True
    assert nodes["policies"].role == "app" and nodes["policies"].entrypoint is False
    assert nodes["policies"].health_port == 7626
    assert nodes["things"].health_port is None
    assert nodes["policies"].mem_limit_mb == 512
    assert nodes["mongodb"].mem_limit_mb == 1024
    assert all(n.chaos_relevant for n in nodes.values())


def test_static_edges_env_depends_on_and_proxy(built):
    _, edges = built
    kinds = {(e.src, e.dst, e.kind) for e in edges}
    assert ("policies", "mongodb", "env") in kinds
    assert ("things", "mongodb", "env") in kinds
    assert ("gateway", "mongodb", "env") in kinds
    assert ("things", "policies", "depends_on") in kinds
    assert ("gateway", "policies", "depends_on") in kinds  # dict form
    assert ("nginx", "gateway", "proxy") in kinds
    assert not any(e.dst == "ditto-ui" for e in edges)  # not a compose service -> no edge
    # secrets never become evidence, and no duplicates
    assert not any("PASSWORD" in e.evidence for e in edges)
    assert len(kinds) == len(edges)
    env_edge = next(e for e in edges if (e.src, e.dst, e.kind) == ("policies", "mongodb", "env"))
    assert "MONGO_DB_HOSTNAME" in env_edge.evidence


def test_documented_edges_only_between_known_nodes(built):
    nodes, _ = built
    knowledge = {"documented_edges": [
        {"src": "gateway", "dst": "things", "evidence": "docs"},
        {"src": "gateway", "dst": "connectivity"},  # unknown dst -> dropped
    ]}
    edges = compose.documented_edges(nodes, knowledge)
    assert [(e.src, e.dst, e.kind, e.evidence) for e in edges] == [("gateway", "things", "documented", "docs")]
    assert compose.documented_edges(nodes, None) == []


def test_knowledge_role_override_and_default_health_port(small_compose):
    nodes = compose.service_nodes(small_compose, {"roles": {"nginx": "other"}, "health_port": 7626})
    assert nodes["nginx"].role == "other" and nodes["nginx"].chaos_relevant is False
    assert nodes["things"].health_port == 7626  # default applied to ditto app images without a healthcheck


def test_criticality_ranks_mongodb_first(built):
    nodes, edges = built
    crit = graph.criticality(nodes, edges)
    assert crit[0].service == "mongodb"
    assert set(crit[0].transitive_dependents) == {"policies", "things", "gateway", "nginx"}
    assert crit[0].fan_in == 3
    scores = [c.score for c in crit]
    assert scores == sorted(scores, reverse=True)
    assert {c.service for c in crit} == set(nodes)


def test_render_mermaid_and_markdown(built):
    nodes, edges = built
    topo = Topology(project="p", services=nodes, edges=edges, criticality=graph.criticality(nodes, edges),
                    findings=["mongodb: not referenced by any depends_on"])
    mm = render.mermaid(topo)
    assert mm.startswith("graph LR")
    assert "svc_mongodb" in mm and "svc_nginx" in mm
    assert "-->|proxy|" in mm or "proxy" in mm
    md = render.markdown(topo)
    assert "## Criticality ranking" in md
    assert "| rank | service |" in md
    assert "| 1 | `mongodb` |" in md
    assert "```mermaid" in md and "graph LR" in md
    assert "mongodb: not referenced" in md


def test_small_parsers():
    assert compose.parse_mem_limit("512m") == 512
    assert compose.parse_mem_limit("1.5g") == 1536
    assert compose.parse_mem_limit(256 * 1024 * 1024) == 256
    assert compose.parse_mem_limit("nonsense") is None
    assert compose.parse_health_port({"test": ["CMD-SHELL", "curl -f http://localhost:8080/health"]}) == 8080
    assert compose.parse_health_port({"test": "true", "disable": True}) is None
    assert compose.parse_env(["A=1", "B", None]) == {"A": "1", "B": ""}
    assert compose.parse_env({"A": None, "B": 2}) == {"A": "", "B": "2"}
