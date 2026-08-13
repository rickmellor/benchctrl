"""On-board LLM as an advisory layer over a run.

Strictly off the control path: deterministic rules are the safety system,
and the model is commentary. See supervisor.py for why that boundary is
where it is.
"""

from __future__ import annotations

__all__ = ["client", "supervisor", "tools"]
