"""Deterministic manifest upkeep and content-bound mechanical proof reuse."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import issue_ledger as L


def command(wt, *args):
    r = subprocess.run(['git','-C',str(wt),*args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def proof_key(wt, base):
    if command(wt,'status','--porcelain').strip():
        return None
    entries = command(wt,'ls-tree','-r','HEAD').splitlines()
    ignored = ('docs/', 'data/manual/')
    manifests = {'tests/all-sources.txt','tests/fork-sources.txt','tests/upstream-modifications.txt','README.md'}
    entries = [e for e in entries if not e.split('\t',1)[-1].startswith(ignored)
               and e.split('\t',1)[-1] not in manifests]
    environment = []
    for path in [Path(wt)/'build/CMakeCache.txt',Path('/usr/bin/cmake'),Path('/usr/bin/c++'),
                 Path(wt)/'third_party/wasmtime/lib/libwasmtime.so']:
        if path.name == 'CMakeCache.txt':
            environment.append(path.read_bytes().hex() if path.exists() else 'missing-cache')
        else:
            environment.append(str(path)+':'+str(path.stat().st_mtime_ns if path.exists() else 'missing'))
    for dependency in [Path('/var/lib/pacman/local'), Path('/var/lib/dpkg/status'),
                       Path('/usr/lib/libQt6Core.so.6'),Path.home()/'.local/zene-deps/root/usr/lib']:
        try:
            st=dependency.stat()
            environment.append(f'{dependency}:{st.st_mtime_ns}:{st.st_size}')
        except OSError:
            environment.append('missing:'+str(dependency))
    environment.append(json.dumps({k:os.environ.get(k) for k in
        ('CC','CXX','CFLAGS','CXXFLAGS','LDFLAGS','PKG_CONFIG_PATH','LD_LIBRARY_PATH')},sort_keys=True))
    payload = [command(wt,'rev-parse',f'{base}^{{tree}}'), *entries, *environment,
               (L.ROOT/'negctl.py').read_text()]
    return hashlib.sha256('\n'.join(payload).encode()).hexdigest()


def artifact_hashes(folder):
    return {name:hashlib.sha256((Path(folder)/name).read_bytes()).hexdigest()
            for name in ('test-reverted.log','test-restored.log')}


def valid_proof(wt, base, proof):
    try:
        key = proof_key(wt, base)
        folder = Path(proof.get('proof_dir',''))
        return bool(key and proof.get('ok') is True and proof.get('proof_key') == key and
                    (folder/'test-restored.log').is_file() and (folder/'test-reverted.log').is_file() and
                    proof.get('artifact_hashes') == artifact_hashes(folder))
    except (OSError, RuntimeError):
        return False


def prove(wt, base, run_dir, report, prover):
    key = proof_key(wt,base)
    path = L.ROOT/'evidence'/f'{key}.json'
    cached = L.read(path) if key else {}
    if valid_proof(wt,base,cached):
        return {**cached,'reused':True}
    result = prover(wt,base,run_dir,report)
    result.update(proof_key=key, proof_dir=str(Path(run_dir)/'negctl'), proof_base=base)
    try:
        result['artifact_hashes'] = artifact_hashes(result['proof_dir'])
    except OSError:
        result['artifact_hashes'] = {}
    if valid_proof(wt,base,result):
        L.write(path,result)
    return result


def housekeeping(wt, base, item):
    """Update factual file registries only; never fabricate reports, evidence or gate baselines."""
    wt = Path(wt)
    if command(wt,'status','--porcelain').strip():
        return []
    source = re.compile(r'\.(?:c|cc|cpp|cxx|h|hpp|py|lua|sh)$')
    excluded = re.compile(r'^(?:src/3rdparty/|third_party/|thirdparty/|tests/reference/|docs/)')
    changed = command(wt,'diff','--name-only',f'{base}..HEAD').splitlines()
    recipe_text = (wt/'tests/all-sources.txt').read_text() if (wt/'tests/all-sources.txt').exists() else ''
    vendor = re.search(r"grep -vE '([^']+)'",recipe_text)
    vendor_skip = re.compile(vendor.group(1)) if vendor else re.compile(r'(?!)')
    added = set(command(wt,'diff','--name-only','--diff-filter=A',f'{base}..HEAD').splitlines())
    originals = {}
    def update(rel, text):
        p = wt/rel
        if p.exists() and p.read_text() != text:
            originals[rel] = p.read_bytes()
            p.write_text(text)
    forkpath = wt/'tests/fork-sources.txt'
    uppath = wt/'tests/upstream-modifications.txt'
    if forkpath.exists() and uppath.exists():
        fork, up = forkpath.read_text(), uppath.read_text()
        registered = {line.split()[0] for line in fork.splitlines() if line.strip() and not line.startswith('#')}
        upstream = {line.split()[0] for line in up.splitlines() if line.strip() and not line.startswith('#')}
        new = sorted(f for f in added if source.search(f) and not excluded.search(f) and not vendor_skip.search(f) and f not in registered and f not in upstream)
        if new:
            fork = fork.rstrip()+'\n'+'\n'.join(new)+'\n'
            registered.update(new)
        inherited = sorted(f for f in changed if source.search(f) and not excluded.search(f) and not vendor_skip.search(f)
                           and f not in added and f not in registered and f not in upstream)
        if inherited:
            reason = str(item.get('title') or ('issue '+L.issue_id(item))).replace('\n',' ')
            up = up.rstrip()+'\n'+'\n'.join(f'{f}\t# {reason}' for f in inherited)+'\n'
        update('tests/fork-sources.txt',fork)
        update('tests/upstream-modifications.txt',up)
    manifest = wt/'tests/all-sources.txt'
    removed = command(wt,'diff','--name-only','--diff-filter=D',f'{base}..HEAD').splitlines()
    listing_changed = any(re.search(r'\.(?:c|cc|cpp|cxx|h|hpp)$',f) for f in added | set(removed))
    if manifest.exists() and (listing_changed or 'tests/all-sources.txt' in changed):
        header=[]
        for line in manifest.read_text().splitlines():
            if line.strip() and not line.startswith('#'):
                break
            header.append(line)
        match = re.search(r"grep -vE '([^']+)'",'\n'.join(header))
        # Honor this tree's own checked recipe, including its vendor exclusions.
        if match and 'git ls-files' in '\n'.join(header):
            skip = re.compile(match.group(1))
            files = command(wt,'ls-files','--','*.c','*.cc','*.cpp','*.cxx','*.h','*.hpp').splitlines()
            files = [f for f in files if not skip.search(f)]
            for extra in ['tests/control-detect-commands.py','tests/control_detect_fixtures.py']:
                if extra in '\n'.join(header) and command(wt,'ls-files','--',extra).strip():
                    files.append(extra)
            update('tests/all-sources.txt','\n'.join(header)+'\n'+'\n'.join(sorted(set(files),key=lambda s:s.encode()))+'\n')
    if not originals:
        return []
    paths = list(originals)
    try:
        command(wt,'add','--',*paths)
        command(wt,'-c','user.name=Zene workflow','-c','user.email=noreply@zene.invalid',
                'commit','--only','-m',f'chore({L.issue_id(item)}): derive source registries from tracked files','--',*paths)
    except Exception:
        for rel, content in originals.items():
            (wt/rel).write_bytes(content)
        command(wt,'restore','--staged','--',*paths)
        raise
    return paths
