"""Shared test helpers: build a cch-home with one approved bootstrap object."""
import copy
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cch.store import Home, store_init
from cch import proposals, reviews, publisher

def make_object(oid='FACT-001', **updates):
    o = {'id': oid, 'type': 'FACT', 'scope': {'domain': 'demo', 'project': 'hub', 'topic': []},
         'status': 'ACTIVE', 'confidence': 'CONFIRMED', 'authority': 'USER_CONFIRMED',
         'priority': 'REFERENCE', 'content': '基线事实 连接池 pool',
         'rationale': None,
         'relations': {'supersedes': [], 'depends_on': [], 'conflicts_with': [], 'derived_from': []},
         'provenance': {'evidence_refs': []},
         'lifecycle': {'valid_from': '2026-09-01', 'valid_until': None, 'ttl_hours': None},
         'retrieval': {'keywords': [], 'aliases': []}, 'tags': []}
    o.update(updates)
    return o

def bootstrap_envelope(pid='PR-boot00000001', objects=None, **extra):
    ops = []
    for obj in (objects or [make_object()]):
        ops.append({'op': 'create', 'object_id': obj['id'], 'expected_object_hash': None, 'after': obj})
    env = {'schema_version': 'proposal/1', 'proposal_id': pid, 'proposal_version': 1,
           'base_revision': '0'*40, 'scope': {'domain': 'demo', 'project': 'hub', 'topic': []},
           'submitter': {'agent_id': 'test-runner'}, 'origin': {'kind': 'manual'},
           'reason': 'test bootstrap', 'operations': ops, 'evidence_assets': None,
           'candidate_assets': [], 'risk_flags': ['BOOTSTRAP'],
           'requested_at': '2026-09-07T00:00:00+00:00'}
    env.update(extra)
    return env

def publish(home, envelope, reviewer='test-reviewer'):
    """Freeze → render → approve → apply in one step; returns apply result."""
    frozen = proposals.freeze(home, copy.deepcopy(envelope))
    bundle = reviews.render_bundle(home, envelope['proposal_id'], envelope['proposal_version'])
    approval = reviews.approve(home, envelope['proposal_id'], envelope['proposal_version'], reviewer)
    return publisher.apply(home, envelope, approval, bundle['bundle_hash'])

def seeded_home(root):
    """A cch-home whose store has exactly one approved bootstrap object."""
    home = Home(root)
    store_init(home)
    publish(home, bootstrap_envelope())
    return home
