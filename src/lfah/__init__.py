"""local-first-agent-harness: a 3-agent (plan -> execute -> evaluate) coding chain.

Runs the heavy executor on a cheap local model, escalates to the cloud only when stuck,
and grades itself with real SWE-bench tests.
"""

__version__ = "0.1.0"
