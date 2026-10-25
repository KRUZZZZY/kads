"""Migrate idle legacy port requests without changing active sessions or accepted work."""
import argparse
from pathlib import Path
import re
import shutil
import time

import issue_ledger as L
import integration_jobs as I
import workflow_report as WR


def migrate(active=(), apply=False):
    active = {L.issue_id(i) for i in active}
    accepted = WR.accepted()
    base = (L.ROOT/'FIX-BASE').read_text().strip()
    results = []
    backup = L.ROOT/'workflow-migration'/str(time.time_ns())
    for path in sorted((L.ROOT/'rework').glob('*.json'), key=lambda p:(L.issue_id(p.stem),p.stem.count('-port'),p.stat().st_mtime)):
        original = path.read_bytes()
        job = L.read(path)
        item = job.get('item') or {}
        alias = str(item.get('cluster',''))
        if not alias.endswith('-port') or item.get('integration') or L.issue_id(item) in active or L.busy(item):
            continue
        # A port with committed corrections is already implementation work.
        # Keep its branch, resume pointer and review feedback intact.
        if any(L.read(p).get('commits') for p in (L.ROOT/'runs').glob(alias+'.r*/record.json')):
            continue
        notes = str(job.get('notes') or '')
        # Preserve accepted source semantics; a normal failed implementation is not an integration request.
        match = re.search(r'accepted as (\S+)',notes)
        prior = alias[:-5]
        candidates = [r for r in accepted if (match and r['branch']==match.group(1)) or r['alias']==prior]
        if not candidates:
            continue
        source = candidates[-1]
        commit = L.git(item.get('worktree','.'),'rev-parse',source['sha'])
        if not commit:
            continue
        candidate = {**source,'cluster':source['alias'],'commit':commit}
        results.append({'old_request':str(path),'issue_id':L.issue_id(item),'accepted_source':commit,'target':base})
        if not apply:
            continue
        if path.read_bytes() != original:
            raise RuntimeError(f'Queue changed during migration: {path}')
        backup.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,backup/path.name)
        I.queue(candidate,notes[:1500] or 'legacy port request',item,base)
        path.unlink()
    return results


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--active',action='append',default=[])
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    import json
    print(json.dumps(migrate(args.active,args.apply),indent=2))
