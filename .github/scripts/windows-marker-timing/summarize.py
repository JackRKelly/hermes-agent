"""Summarize real durable timing records; partial calls stay incomplete."""
import argparse
import json
from pathlib import Path


def summarize(root):
    records = []
    truncated_lines = []
    for path in sorted(Path(root).glob('timing-*.jsonl')):
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                truncated_lines.append({'file': str(path), 'line': number})
    starts = {r['invocation']: r for r in records if r['event'] == 'helper_start'}
    ends = {r['invocation']: r for r in records if r['event'] in ('helper_end', 'helper_error')}
    helpers = []
    for invocation, start in starts.items():
        end = ends.get(invocation)
        helpers.append(dict(nodeid=start['nodeid'], helper=start['helper'],
                            implementation=start['implementation'],
                            status=end['event'] if end else 'incomplete',
                            seconds=end['elapsed_ns'] / 1000000000 if end else None,
                            invocation=invocation))
    return dict(status='diagnostic-only-not-acceptance', helper_calls=helpers,
                selected=[r['selected_nodeids'] for r in records if r['event'] == 'collection'],
                phases=[r for r in records if r['event'] == 'phase_end'],
                incomplete_invocations=sorted(set(starts) - set(ends)),
                orphan_ends=sorted(set(ends) - set(starts)), malformed_lines=truncated_lines,
                record_count=len(records))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.directory), indent=2, sort_keys=True))
