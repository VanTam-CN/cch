"""Materialized read snapshots of approved commits.

One task entry resolves the approved ref exactly once and reads every object
and evidence file from that single commit. Snapshot cache directories are
built fully, verified, then atomically renamed into place; readers verify the
manifest instead of trusting a directory that merely exists.
"""
from __future__ import annotations
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from .core import Base, CCHError, digest
from . import store

def _manifest(dir_path, commit):
    files = {}
    for p in sorted(dir_path.rglob('*')):
        if p.is_file() and p.name != 'manifest.json':
            files[str(p.relative_to(dir_path))] = digest(p.read_bytes())
    return {'format': 'cch-snapshot/1', 'commit': commit, 'files': files}

def _verify(dir_path, commit):
    manifest_path = dir_path/'manifest.json'
    if not manifest_path.is_file():
        return False
    import json
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeError):
        return False
    if manifest.get('format') != 'cch-snapshot/1' or manifest.get('commit') != commit:
        return False
    listed = manifest.get('files')
    if not isinstance(listed, dict):
        return False
    actual = {str(p.relative_to(dir_path)) for p in dir_path.rglob('*')
              if p.is_file() and p.name != 'manifest.json'}
    if set(listed) != actual:
        return False
    return all(digest((dir_path/rel).read_bytes()) == sha for rel, sha in listed.items())

def materialize(home, commit):
    """Return a verified snapshot directory for an approved commit."""
    final = home.cache/commit
    if final.is_dir() and _verify(final, commit):
        return final
    if final.exists():
        aside = home.cache/f'.corrupt-{uuid.uuid4().hex[:8]}-{commit}'
        try:
            os.rename(final, aside)
            shutil.rmtree(aside, ignore_errors=True)
        except OSError:
            pass  # another reader is rebuilding; fall through and retry below
    tmp = Path(tempfile.mkdtemp(prefix=f'.tmp-{commit[:12]}-', dir=home.cache))
    try:
        index = tempfile.mktemp(prefix='cch-snapindex-')
        try:
            store.git(home, 'read-tree', commit, index=index)
            store.git(home, 'checkout-index', '--all', '--force', f'--prefix={str(tmp)}/',
                      index=index, work_tree=tmp)
        finally:
            Path(index).unlink(missing_ok=True)
        import json
        manifest = _manifest(tmp, commit)
        (tmp/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1) + '\n', encoding='utf-8')
        try:
            os.rename(tmp, final)
        except OSError:
            if final.is_dir() and _verify(final, commit):
                shutil.rmtree(tmp, ignore_errors=True)
                return final
            raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    if not _verify(final, commit):
        raise CCHError('SNAPSHOT_INVALID', f'Snapshot verification failed for {commit[:12]}')
    return final

def load_base(home, revision=None):
    """Resolve one revision and load the whole Base from it."""
    commit = store.resolve_revision(home, revision)
    if commit is None:
        raise CCHError('EMPTY_STORE', 'Store has no approved cognition yet; nothing to compile')
    return commit, Base(materialize(home, commit))
