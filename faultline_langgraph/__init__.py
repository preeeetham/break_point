"""Faultline agents reimplemented on LangGraph + LangChain -- an opt-in, parallel implementation.

Reads and writes the same reports/ blackboard and validates against the same models in faultline.schemas as
the original agents; nothing in faultline/ imports this package. Install with `pip install -e '.[langgraph]'`,
run with `python -m faultline_langgraph.cli ...`. See faultline_langgraph/README.md for what is converted.
"""
