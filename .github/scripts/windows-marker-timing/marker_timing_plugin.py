"""Diagnostic pytest plugin: wrap ORIGINAL helpers; no replacement or timeout edits."""
import functools
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time
import uuid

_SESSION = uuid.uuid4().hex
_NODE = None


def emit(event, **fields):
    root = Path(os.environ['MARKER_TIMING_DIR']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    record = dict(event=event, session=_SESSION, pid=os.getpid(),
                  wall_ns=time.time_ns(), monotonic_ns=time.perf_counter_ns(),
                  nodeid=_NODE, **fields)
    # Per-process output, flushed AND fsynced at every boundary; survives outer kill.
    with (root / f'timing-{os.getpid()}-{_SESSION}.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def timed(original, label):
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        invocation = uuid.uuid4().hex
        emit('helper_start', helper=label, implementation=original.__module__, invocation=invocation,
             owner_pid=args[0] if label == '_creation_time' else None)
        start = time.perf_counter_ns()
        try:
            result = original(*args, **kwargs)
        except BaseException as error:
            emit('helper_error', helper=label, invocation=invocation,
                 elapsed_ns=time.perf_counter_ns() - start, error_type=type(error).__name__)
            raise
        elapsed = time.perf_counter_ns() - start
        details = {'identity': result} if label == '_creation_time' else {
            'child_pid': result[0], 'returncode': result[1], 'stdout_chars': len(result[2])}
        emit('helper_end', helper=label, invocation=invocation, elapsed_ns=elapsed, **details)
        return result
    return wrapped


def pytest_addoption(parser):
    parser.addini('marker_timing_dir', 'Required durable diagnostic output directory')


def bind_selected_helpers(items, root, originals, wrappers) -> list[str]:
    """Bind execution globals even when pytest imports a test by its bare name."""
    aliases = []
    root = Path(root).resolve()
    for item in items:
        relative = item.nodeid.split('::', 1)[0].replace('\\', '/')
        expected = (root / relative).resolve()
        namespace = getattr(item.obj, '__globals__', None)
        if (not relative.endswith('.py') or not expected.is_relative_to(root)
                or namespace is None or Path(namespace.get('__file__', '')).resolve() != expected):
            raise RuntimeError('Selected function globals do not match the actual source path')
        canonical = relative[:-3].replace('/', '.')
        for helper, original in originals.items():
            if namespace.get(helper) is original:
                namespace[helper] = wrappers[helper]
            if namespace.get(helper) is wrappers[helper]:
                aliases.append(f'{canonical}.{helper}')
    return aliases


def pytest_collection_finish(session):
    # Final selected items only: modifyitems runs before pytest's -k deselection.
    config, items = session.config, session.items
    directory = config.getini('marker_timing_dir')
    if not directory:
        raise RuntimeError('pass -o marker_timing_dir=ABSOLUTE_ARTIFACT_DIRECTORY')
    if not Path(directory).is_absolute():
        raise RuntimeError('marker_timing_dir must be absolute')
    os.environ['MARKER_TIMING_DIR'] = directory
    if sys.platform != 'win32':
        raise RuntimeError('native Windows required; instrumentation must not spoof the host')
    from tests.scripts.desktop_update import test_desktop_update_windows_marker as source
    originals = {name: getattr(source, name) for name in ('_creation_time', '_run')}
    wrappers = {name: timed(fn, name) for name, fn in originals.items()}
    patched = []
    # Handle `from module import helper` aliases already bound during collection.
    # Calls still execute each original function with original args/kwargs/exceptions.
    for name, module in list(sys.modules.items()):
        if name.startswith('tests.scripts.desktop_update.') and module is not None:
            for attr, value in list(vars(module).items()):
                for helper, original in originals.items():
                    if value is original:
                        setattr(module, attr, wrappers[helper])
                        patched.append(f'{name}.{attr}')
    patched.extend(bind_selected_helpers(items, config.rootpath, originals, wrappers))
    emit('collection', platform=sys.platform, machine=platform.machine(),
         executable=sys.executable, helper_source_sha256=hashlib.sha256(
             Path(source.__file__).read_bytes()).hexdigest(),
         selected_nodeids=[item.nodeid for item in items], wrapped_aliases=patched)


def pytest_runtest_logstart(nodeid, location):
    global _NODE
    _NODE = nodeid
    emit('test_start')


def pytest_runtest_logreport(report):
    emit('phase_end', reported_nodeid=report.nodeid, phase=report.when,
         outcome=report.outcome, duration_seconds=report.duration)


def pytest_runtest_logfinish(nodeid, location):
    global _NODE
    emit('test_end', reported_nodeid=nodeid)
    _NODE = None
