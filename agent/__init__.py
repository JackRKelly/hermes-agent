"""Agent internals extracted from run_agent.py so it stays focused on AIAgent."""

import sys

# The protected entry loads its latch BEFORE this package. Do not add imports to
# the ordinary managed/sentinel entry's pinned bootstrap closure.
if not getattr(sys.modules.get('hermes_protected'), 'active', lambda: False)():
    from . import jiter_preload as _jiter_preload
