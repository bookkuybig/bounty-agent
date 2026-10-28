"""Numeric pre-filter for paid open-source issues.

Stage 1 collects candidates, stage 2 scores them with arithmetic only.
Nothing here calls an LLM; the whole point is to spend tokens on the
small slice of issues that survive.
"""

__version__ = "0.1.0"
