"""The demo's flagd file, as seen by the Action API mock (rollback_release) and the practice Simulator only.

The agents and the telemetry layer must never import this module (REQUIREMENTS R1); a test enforces it.
On the real system "roll back the faulty release" is a deployment; on the demo the faulty release *is* the flag, so
the mock reverts it here. flagd watches the file and picks the change up within seconds.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

FLAG_FILE = Path("src") / "flagd" / "demo.flagd.json"

# Which flags make up each service's "faulty release" (mock only).
SERVICE_FLAGS: dict[str, tuple[str, ...]] = {
    "payment": ("paymentFailure", "paymentUnreachable"),
    "checkout": ("paymentUnreachable", "kafkaQueueProblems"),
    "cart": ("cartFailure", "cartMetricCardinalityOverflow"),
    "product-catalog": ("productCatalogFailure", "productCatalogLockContention"),
    "recommendation": ("recommendationCacheFailure",),
    "ad": ("adFailure", "adHighCpu", "adManualGc"),
    "email": ("emailMemoryLeak",),
    "image-provider": ("imageSlowLoad",),
    "shipping": ("intlShippingSlowdown",),
    "quote": ("intlShippingSlowdown",),
    "frontend": ("loadGeneratorFloodHomepage",),
    "frontend-proxy": ("loadGeneratorFloodHomepage",),
    "load-generator": ("loadGeneratorFloodHomepage",),
    "accounting": ("kafkaConsumerDead", "kafkaQueueProblems"),
    "fraud-detection": ("kafkaConsumerDead", "kafkaQueueProblems"),
}

_lock = threading.Lock()


class FlagFile:
    def __init__(self, demo_dir: str | Path):
        self.path = Path(demo_dir).expanduser() / FLAG_FILE

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.path)

    def variants(self) -> dict[str, str]:
        return {k: v.get("defaultVariant") for k, v in self._read().get("flags", {}).items()}

    def set(self, values: dict[str, str]) -> dict[str, str]:
        """Set defaultVariant per flag; returns the previous values (the undo)."""
        with _lock:
            data = self._read()
            prev = {}
            for k, variant in values.items():
                flag = data.get("flags", {}).get(k)
                if flag is None:
                    raise KeyError(f"unknown flag {k}")
                if variant not in (flag.get("variants") or {}):
                    raise ValueError(f"flag {k} has no variant {variant!r}")
                prev[k] = flag.get("defaultVariant")
                flag["defaultVariant"] = variant
            self._write(data)
            return prev

    def off_variant(self, flag: str) -> str:
        variants = (self._read()["flags"][flag].get("variants") or {})
        for name in ("off", "false", "disabled"):
            if name in variants:
                return name
        for name, value in variants.items():
            if value in (False, 0, "off"):
                return name
        return next(iter(variants))

    def faulty(self, service: str) -> dict[str, str]:
        """Flags of this service's release that are not at their off variant."""
        current = self.variants()
        return {f: current[f] for f in SERVICE_FLAGS.get(service, ()) if f in current and current[f] != self.off_variant(f)}
