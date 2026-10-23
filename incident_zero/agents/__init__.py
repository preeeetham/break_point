"""The nine agents. Each is a small class with one job; the orchestrator moves an incident between them.

These modules must never import incident_zero.actions.flags, incident_zero.practice or Break Point's reports and
chaos code (REQUIREMENTS R1, R3); tests/test_actions.py enforces it.
"""
