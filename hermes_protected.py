"""Fail-closed SOURCE import closure for the experimental protected conversation.

Not a Python sandbox or kernel admission. Call only in a fresh -I -S -B process,
BEFORE importing agent/bootstrap/config. No environment/profile switch enables it.
The production managed launch remains refused by session_protected_tools.
"""
import importlib.machinery
from pathlib import Path
import sys
import sysconfig

_FROZEN = False
_SOURCE = Path(__file__).resolve().parent
_STDLIB = Path(sysconfig.get_path('stdlib')).resolve()
_ALLOWED = frozenset({'hermes_protected', 'agent', 'agent.protected_conversation',
                      'agent.protected_ipc', 'agent.transports', 'agent.transports.types'})


def active():
    return _FROZEN


def _permitted(name, origin):
    if origin in ('built-in', 'frozen'):
        return True
    if not origin:
        return False
    path = Path(origin).resolve()
    if name in _ALLOWED:
        expected = _SOURCE.joinpath(*name.split('.'))
        return path in (expected.with_suffix('.py'), expected / '__init__.py')
    return path.is_relative_to(_STDLIB) and not {'site-packages', 'dist-packages'} & set(path.parts)


class _FrozenImports:
    def find_spec(self, fullname, path=None, target=None):
        # Resolve, inspect the ACTUAL origin, then return that same spec. Name-level
        # stdlib allowlists permit a workspace json.py to execute before detection.
        spec = (importlib.machinery.BuiltinImporter.find_spec(fullname, path, target)
                or importlib.machinery.FrozenImporter.find_spec(fullname, path, target)
                or importlib.machinery.PathFinder.find_spec(fullname, path, target))
        if spec is None or not _permitted(fullname, spec.origin):
            raise ImportError('protected import denied: ' + fullname)
        return spec


def freeze():
    """Irreversible cooperative source latch, rejecting already-discovered runtimes."""
    global _FROZEN
    if _FROZEN:
        raise RuntimeError('protected imports already frozen')
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError('fresh -I -S -B interpreter required')
    if sys.meta_path != [importlib.machinery.BuiltinImporter, importlib.machinery.FrozenImporter,
                         importlib.machinery.PathFinder]:
        raise RuntimeError('unexpected import discovery machinery')
    for name, module in tuple(sys.modules.items()):
        if name == '__main__':
            continue
        spec = getattr(module, '__spec__', None)
        if not _permitted(name, getattr(spec, 'origin', None)):
            raise RuntimeError('already imported outside protected closure: ' + name)
    sys.meta_path.insert(0, _FrozenImports())
    _FROZEN = True
