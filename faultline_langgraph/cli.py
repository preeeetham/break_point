"""Opt-in CLI for the LangGraph agents. A separate command on purpose: `faultline plan` is untouched and never
switches engine behind your back; this is what you run when you choose the LangGraph implementation.

    python -m faultline_langgraph.cli plan [--no-llm] [--allow-protected] [--trace] [--json]
    python -m faultline_langgraph.cli graph            # the planner graph as Mermaid
"""
from __future__ import annotations

import argparse
import sys


def _say(msg: str, to_stderr: bool = False) -> None:
    print(msg, file=sys.stderr if to_stderr else sys.stdout, flush=True)


def cmd_plan(s, store, a) -> int:
    # The planner is built by the original CLI's own helper, so topology, policy, history, helper images and
    # (on newer branches) any extra ranking inputs are exactly what `faultline plan` would use.
    from faultline.cli import Session, _planner
    from faultline.planning.planner import describe_candidates

    from .llm import chat_model_from_settings
    from .planner import plan_next

    def say(msg: str) -> None:
        _say(msg, to_stderr=a.json)  # keep stdout clean for the JSON spec

    sess = Session(s, store, need_probes=False)
    planner = _planner(sess, False, a.allow_protected)  # original LLMClient detached; the agent proposes instead
    handle = chat_model_from_settings(s, enabled=not a.no_llm)
    say(f"engine: LangGraph planner agent, model {handle.label}"
        + ("" if handle.model is not None else f" not used ({handle.reason}) -> deterministic plan"))
    result = plan_next(planner, handle, store.baseline())
    say(describe_candidates(result.candidates))
    if a.trace:
        say("\nagent trace:")
        for line in result.trace:
            say(f"  - {line}")
    spec = result.spec
    if spec:
        say(f"\nnext experiment ({spec.planned_by}): {spec.failure_type} on {spec.target_service} {spec.parameters}")
        say(f"  why: {spec.rationale}\n  hypothesis: {spec.hypothesis.statement}")
        for n in spec.planner_notes:
            say(f"  note: {n}")
        if a.json:
            _say(spec.model_dump_json(indent=2))
    return 0


def cmd_graph(s, store, a) -> int:
    from .llm import ModelHandle
    from .planner import build_graph

    _say(build_graph(None, ModelHandle(None, "diagram")).get_graph().draw_mermaid())
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m faultline_langgraph.cli",
                                description="Faultline agents on LangGraph (opt-in; `faultline` itself is unchanged)")
    p.add_argument("--config", help="settings yaml (default config/settings.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="rank candidates and let the planning agent pick the next experiment, without running it")
    pl.add_argument("--no-llm", action="store_true", help="deterministic plan only, identical to `faultline plan --no-llm`")
    pl.add_argument("--allow-protected", action="store_true")
    pl.add_argument("--trace", action="store_true", help="print the agent's steps: tool calls, rejections, fallback")
    pl.add_argument("--json", action="store_true", help="print the ExperimentSpec as JSON on stdout (everything else to stderr)")
    pl.set_defaults(fn=cmd_plan)
    sub.add_parser("graph", help="print the planner graph as Mermaid").set_defaults(fn=cmd_graph)

    a = p.parse_args(argv)
    try:
        import langgraph  # noqa: F401
    except ImportError:
        _say("faultline_langgraph needs the optional dependencies: pip install -e '.[langgraph]'", to_stderr=True)
        return 2

    from faultline.config import load_settings
    from faultline.reporting.store import ReportStore

    s = load_settings(a.config)
    try:
        return a.fn(s, ReportStore(s.reports), a)
    except KeyboardInterrupt:
        _say("\ninterrupted", to_stderr=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
