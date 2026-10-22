"""Break Point agents reimplemented on LangGraph + LangChain -- an opt-in, parallel implementation.

Reads and writes the same reports/ blackboard and validates against the same models in break_point.schemas as
the original agents; nothing in break_point/ imports this package. Install with `pip install -e '.[langgraph]'`,
run with `python -m break_point_langgraph.cli ...`. See break_point_langgraph/README.md for what is converted.
"""
