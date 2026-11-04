"""Fixed SOURCE-only fresh entry; NOT a privileged/root-installed launcher."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hermes_protected import freeze

freeze()
from agent.protected_ipc import conversation_main

if __name__ == '__main__':
    raise SystemExit(conversation_main())
