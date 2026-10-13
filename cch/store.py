"""Dedicated Git object store holding approved cognition history.

The approved ref (refs/heads/cch-approved) is the only formal version pointer.
All git access goes through subprocess argv arrays with neutralized config;
no hooks, no shell, no proposal-controlled paths or refs.
"""
from __future__ import annotations
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .core import CCHError, digest

APPROVED_REF = 'refs/heads/cch-approved'
GIT_IDENTITY = {'GIT_AUTHOR_NAME': 'CCH Publisher', 'GIT_AUTHOR_EMAIL': 'cch@local',
                'GIT_COMMITTER_NAME': 'CCH Publisher', 'GIT_COMMITTER_EMAIL': 'cch@local'}

def canon(value):
    """Canonical JSON encoding: UTF-8, sorted keys, no padding spaces, no NaN."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def canon_digest(value):
    return digest(canon(value).encode())

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

class Home:
    """Layout manager for a cch-home working directory."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.git_dir = self.root/'store.git'
        self.sources = self.root/'sources'
        self.extracted = self.root/'extracted'
        self.jobs = self.root/'jobs'
        self.submissions = self.root/'submissions'
        self.proposals = self.root/'proposals'
        self.reviews = self.root/'reviews'
        self.runs = self.root/'runs'
        self.cache = self.root/'cache'/'snapshots'
        self.locks = self.root/'locks'
        self.settings = self.root/'settings.yaml'
        for path in [self.git_dir, self.sources/'sha256', self.extracted, self.jobs,
                     self.submissions, self.proposals, self.reviews, self.runs, self.cache, self.locks]:
            path.mkdir(parents=True, exist_ok=True)

    def require_store(self):
        if not (self.git_dir/'HEAD').is_file():
            raise CCHError('NO_STORE', f'{self.git_dir} is not an initialized store; run store init')
        return self

def git(home, *args, check=True, index=None, work_tree=None, env_extra=None):
    """Run one git plumbing command against the bare store with controlled config."""
    cmd = ['git', '--git-dir', str(home.git_dir)]
    if index is not None:
        cmd += ['--work-tree', str(work_tree)] if work_tree else []
    env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null',
               GIT_CONFIG_NOSYSTEM='1', **GIT_IDENTITY)
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(cmd + list(args), capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise CCHError('GIT_ERROR', f'git {" ".join(args[:3])}: {result.stderr.strip() or result.returncode}')
    return result

def store_init(home):
    if (home.git_dir/'HEAD').exists():
        raise CCHError('STORE_EXISTS', 'store.git already initialized')
    subprocess.run(['git', 'init', '--bare', '--quiet', str(home.git_dir)], check=True,
                   env=dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null'))
    for key, value in [('core.autocrlf', 'false'), ('core.hooksPath', ''), ('core.logallrefupdates', 'false')]:
        git(home, 'config', key, value)
    return {'state': 'EMPTY', 'ref': None}

def approved_commit(home):
    """Current formal revision, or None for an empty (never-published) store."""
    home.require_store()
    result = git(home, 'rev-parse', '--verify', '--quiet', APPROVED_REF + '^{commit}', check=False)
    return result.stdout.strip() or None

def history(home):
    home.require_store()
    result = git(home, 'rev-list', '--reverse', APPROVED_REF, check=False)
    return result.stdout.split() if result.returncode == 0 else []

def resolve_revision(home, revision):
    """Only commits reachable from the approved ref are readable history."""
    home.require_store()
    if revision is None:
        return approved_commit(home)
    for commit in history(home):
        if commit == revision:
            return commit
    raise CCHError('INVALID_REVISION', 'Revision is not part of approved history')

def write_tree_from_dir(home, tree_dir):
    """Build a tree object from a complete working directory using a private index."""
    index = tempfile.mktemp(prefix='cch-index-')
    try:
        git(home, 'read-tree', '--empty', index=index)
        git(home, 'add', '--all', '--', '.', index=index, work_tree=tree_dir)
        result = git(home, 'write-tree', index=index, work_tree=tree_dir)
        return result.stdout.strip()
    finally:
        Path(index).unlink(missing_ok=True)

def make_commit(home, tree, parent, message, date=None):
    env = {'GIT_AUTHOR_DATE': date or now_iso(), 'GIT_COMMITTER_DATE': date or now_iso()}
    args = ['commit-tree', tree, '-m', message]
    if parent:
        args += ['-p', parent]
    return git(home, *args, env_extra=env).stdout.strip()

def cas_update_ref(home, new_commit, old_commit):
    """Compare-and-swap the approved ref; the zero id asserts the ref must not exist yet."""
    expected = old_commit or '0'*40
    result = git(home, 'update-ref', APPROVED_REF, new_commit, expected, check=False)
    if result.returncode != 0:
        raise CCHError('CAS_FAILED', f'approved ref moved or update rejected: {result.stderr.strip()}')

def ls_tree_files(home, commit):
    """All file paths stored in a commit tree."""
    result = git(home, 'ls-tree', '-r', '--name-only', commit)
    return [line for line in result.stdout.splitlines() if line]

def cat_file(home, revision, path):
    return git(home, 'show', f'{revision}:{path}', check=False)

@contextmanager
def publish_lock(home, timeout_seconds=30):
    """Advisory flock serializing publishers; fails loudly instead of waiting forever."""
    lock_path = home.locks/'publish.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = datetime.now(timezone.utc).timestamp() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if datetime.now(timezone.utc).timestamp() >= deadline:
                    raise CCHError('LOCK_TIMEOUT', 'Another publisher holds the publish lock')
                import time; time.sleep(0.05)
        yield
    finally:
        os.close(fd)

def store_status(home):
    home.require_store()
    commit = approved_commit(home)
    info = {'state': 'EMPTY' if commit is None else 'OK', 'approved_revision': commit,
            'history_length': len(history(home)), 'mode': 'cooperative',
            'enforced': False,
            'enforced_note': ('cooperative: every program runs as the same OS user; workflow control '
                              'is tool-level, not an OS-level human-only guarantee')}
    if commit:
        info['tree_files'] = len(ls_tree_files(home, commit))
    return info

def store_export(home, target_dir):
    """Full backup: git history bundle plus every source blob referenced by approved provenance."""
    home.require_store()
    commit = approved_commit(home)
    if commit is None:
        raise CCHError('EMPTY_STORE', 'Nothing to export from an empty store')
    target = Path(target_dir)
    if target.exists() and any(target.iterdir()):
        raise CCHError('TARGET_NOT_EMPTY', 'Export target directory must be empty or absent')
    target.mkdir(parents=True, exist_ok=True)
    bundle = target/'history.bundle'
    result = git(home, 'bundle', 'create', str(bundle), APPROVED_REF)
    if result.returncode != 0:
        raise CCHError('GIT_ERROR', f'bundle create failed: {result.stderr.strip()}')
    referenced = set()
    for path in ls_tree_files(home, commit):
        if path.startswith('provenance/') and path.endswith('.json'):
            try:
                record = json.loads(git(home, 'show', f'{commit}:{path}').stdout)
            except (json.JSONDecodeError, CCHError):
                raise CCHError('GIT_ERROR', f'corrupt provenance in approved tree: {path}')
            sha = record.get('source_sha256') if isinstance(record, dict) else None
            if isinstance(sha, str):
                referenced.add(sha)
    sources_meta = {}
    (target/'sources').mkdir()
    for sha in sorted(referenced):
        blob = home.sources/'sha256'/sha
        if not blob.is_file() or digest(blob.read_bytes()) != sha:
            raise CCHError('SOURCE_CHANGED', f'Referenced source blob missing or corrupt: {sha}')
        shutil.copy2(blob, target/'sources'/sha)
        sources_meta[sha] = True
    manifest = {'format': 'cch-export/1', 'approved_revision': commit,
                'sources': sorted(referenced), 'created_at': now_iso(),
                'bundle_sha256': digest(bundle.read_bytes())}
    (target/'export-manifest.json').write_text(canon(manifest) + '\n', encoding='utf-8')
    return manifest

def store_restore(home, package_dir, recompile=None):
    """Restore into a NEW empty home. Refuses to overwrite, verifies hashes and revision."""
    package = Path(package_dir)
    manifest_path = package/'export-manifest.json'
    if not manifest_path.is_file():
        raise CCHError('VALIDATION_ERROR', 'Not an export package: export-manifest.json missing')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    require(manifest.get('format') == 'cch-export/1', 'Unknown export format')
    if home.git_dir.exists() and any(home.git_dir.iterdir()):
        raise CCHError('TARGET_NOT_EMPTY', 'Restore requires an uninitialized target store')
    bundle = package/'history.bundle'
    require(bundle.is_file() and digest(bundle.read_bytes()) == manifest['bundle_sha256'], 'Bundle hash mismatch')
    for sha in manifest['sources']:
        blob = package/'sources'/sha
        require(blob.is_file() and digest(blob.read_bytes()) == sha, f'Missing or corrupt source in package: {sha}')
    subprocess.run(['git', 'clone', '--quiet', '--bare', str(bundle), str(home.git_dir)], check=True,
                   capture_output=True, env=dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null'))
    restored = approved_commit(home)
    require(restored == manifest['approved_revision'], 'Restored revision differs from manifest')
    for sha in manifest['sources']:
        shutil.copy2(package/'sources'/sha, home.sources/'sha256'/sha)
    return {'restored_revision': restored, 'sources_restored': len(manifest['sources'])}

def require(ok, message):
    if not ok:
        raise CCHError('VALIDATION_ERROR', message)

def new_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:12]}'
