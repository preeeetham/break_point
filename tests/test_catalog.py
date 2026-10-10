import pytest

from faultline.chaos.catalog import CATALOG, applicable, clamp, reproduce_commands
from faultline.schemas import ServiceNode


def test_every_failure_type_has_duration_param():
    for key, ft in CATALOG.items():
        names = [p.name for p in ft.params]
        assert "duration_s" in names, key
        assert ft.key == key


def test_clamp_bounds_and_notes():
    ft = CATALOG["network_delay"]
    out, notes = clamp(ft, {"duration_s": 1, "delay_ms": 99999, "jitter_ms": 50})
    assert out["duration_s"] == 10 and out["delay_ms"] == 3000 and out["jitter_ms"] == 50
    assert any("duration_s raised" in n for n in notes)
    assert any("delay_ms lowered" in n for n in notes)
    assert isinstance(out["duration_s"], int)


def test_clamp_uses_defaults_and_flags_invalid_values():
    ft = CATALOG["network_loss"]
    out, notes = clamp(ft, {"loss_percent": "lots"})
    assert out == {"duration_s": 45, "loss_percent": 30}
    assert any("invalid" in n for n in notes)


@pytest.mark.parametrize("raw,expected", [("true", True), ("yes", True), ("1", True), ("false", False),
                                          ("no", False), (True, True), (0, False)])
def test_clamp_bool_param_from_strings(raw, expected):
    out, _ = clamp(CATALOG["container_crash"], {"block_restart": raw})
    assert out["block_restart"] is expected


def test_clamp_ignores_unknown_params_with_note():
    out, notes = clamp(CATALOG["cpu_stress"], {"bogus": 1, "workers": 2})
    assert "bogus" not in out
    assert any("ignored unknown parameters" in n and "bogus" in n for n in notes)


def _node(name, image, role, chaos_relevant=True):
    return ServiceNode(name=name, image=image, role=role, chaos_relevant=chaos_relevant)


def test_applicable_traffic_spike_only_on_nginx_or_gateway():
    ft = CATALOG["traffic_spike"]
    assert applicable(ft, _node("nginx", "nginx:1.27", "proxy"))
    assert applicable(ft, _node("gateway", "eclipse/ditto-gateway", "app"))
    assert not applicable(ft, _node("things", "eclipse/ditto-things", "app"))


def test_applicable_db_flood_only_on_mongo_image():
    ft = CATALOG["db_flood"]
    assert applicable(ft, _node("mongodb", "mongo:7.0", "datastore"))
    assert not applicable(ft, _node("pg", "postgres:16", "datastore"))
    assert not applicable(ft, _node("things", "eclipse/ditto-things", "app"))


def test_applicable_never_targets_ui_and_datastore_crash_is_policy_gated():
    ui = _node("ditto-ui", "eclipse/ditto-ui", "ui", chaos_relevant=False)
    assert not any(applicable(ft, ui) for ft in CATALOG.values())
    mongo = _node("mongodb", "mongo:7.0", "datastore")
    # the catalog allows it; the Day-1 policy (config/thresholds.yaml) is what blocks it unless --allow-protected
    assert applicable(CATALOG["container_crash"], mongo)
    assert applicable(CATALOG["network_delay"], mongo)
    assert not applicable(CATALOG["traffic_spike"], mongo)


def test_reproduce_commands_for_every_type_mention_container():
    for key, ft in CATALOG.items():
        params, _ = clamp(ft, {"block_restart": True})
        cmds = reproduce_commands(key, "bosch-ditto-x-1", params, "bosch-ditto_default", "pumba:img", "tc:img")
        assert cmds, key
        joined = "\n".join(cmds)
        if key != "traffic_spike":  # runs from the host against the public URL, not a container
            assert "bosch-ditto-x-1" in joined, key
    assert reproduce_commands("does_not_exist", "c", {}, "n", "p", "t") == []
    crash = reproduce_commands("container_crash", "c1", {"duration_s": 30, "block_restart": True}, "n", "p", "t")
    assert crash[0] == "docker update --restart=no c1"
