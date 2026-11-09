"""Compatibility alias for the Wikimedia adapter's previous module path.

The module object is shared so existing imports and patches of module globals
continue to affect the one implementation.
"""
import sys
from app.sources.wikimedia import adapter as _implementation

sys.modules[__name__] = _implementation
