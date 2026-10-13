"""End-to-end acceptance run on an ISOLATED acceptance store (never the user's real store).

Flow: store init -> bootstrap publish -> source add (md/docx/pdf/log) -> ingest
prepare/export-job -> import recorded fixtures for md/docx/pdf/log
-> proposal from candidates -> review
-> approve -> apply -> two independent context calls read the same revision -> export/
restore -> recompile. Fixture approvals here prove the functional chain only;
they are NOT authorization of real business content.

Run: .venv/bin/python tools/e2e_acceptance.py [--out output/e2e-acceptance]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
FIX = ROOT/'tests'/'fixtures'/'import'

def cch(home, *args, expect_ok=True):
    result = subprocess.run([PYTHON, '-m', 'cch', '--home', str(home), *[str(a) for a in args]],
                            capture_output=True, text=True, cwd=ROOT)
    if expect_ok and result.returncode != 0:
        raise SystemExit(f'CLI failed: cch {" ".join(map(str, args))}\n{result.stderr}')
    return json.loads(result.stdout) if result.stdout.strip() else {}

def write_result(home, job, candidates, blocks_status, model):
    path = Path(home)/'jobs'/job['job_id']/'result.json'
    path.write_text(json.dumps({
        'schema_version': 'exchange-result/1', 'job_id': job['job_id'],
        'manifest_hash': job['manifest_hash'], 'blocks': blocks_status,
        'candidates': candidates, 'model_meta': model}, ensure_ascii=False, indent=2), encoding='utf-8')
    return path

def processed(job):
    return [{'block_id': b['block_id'], 'status': 'PROCESSED'} for b in job['blocks']]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='output/e2e-acceptance')
    args = parser.parse_args()
    out = ROOT/args.out
    if out.exists():
        raise SystemExit('Output already exists; choose a new --out directory. Nothing was deleted.')
    out.mkdir(parents=True)
    home = out/'cch-home'
    report = {'steps': []}

    def step(name, detail):
        report['steps'].append({'step': name, **detail})
        print(f'== {name}: {json.dumps(detail, ensure_ascii=False, sort_keys=True)[:160]}')

    cch(home, 'store', 'init')
    step('store-init', cch(home, 'store', 'status'))

    bootstrap = json.loads((ROOT/'samples'/'bootstrap-proposal.json').read_text(encoding='utf-8'))
    bootstrap_path = out/'bootstrap-proposal.json'
    bootstrap_path.write_text(json.dumps(bootstrap, ensure_ascii=False, indent=2), encoding='utf-8')
    cch(home, 'proposal', 'create', '--file', bootstrap_path)
    cch(home, 'review', 'render', bootstrap['proposal_id'])
    cch(home, 'review', 'approve', bootstrap['proposal_id'], '--reviewer', 'acceptance-runner')
    step('bootstrap-apply', cch(home, 'apply', bootstrap['proposal_id']))

    sources = {}
    for name, declared in [('demo-project-brief.md', '2026-08-30'), ('demo-config-table.docx', None),
                           ('demo-notes.pdf', None), ('demo-worklog.log', None)]:
        extra = ['--declared-date', declared] if declared else []
        sources[name] = cch(home, 'source', 'add', FIX/name, '--domain', 'demo', '--project', 'hub', *extra)
    step('sources-added', {k: v['source_id'] for k, v in sources.items()})

    jobs = {}
    for name, added in sources.items():
        prepare = cch(home, 'ingest', 'prepare', added['source_id'])
        job = cch(home, 'ingest', 'export-job', added['source_id'], '--chunk', 0)
        job_data = json.loads(Path(job['job_path']).read_text(encoding='utf-8'))
        jobs[name] = job_data
    step('jobs-exported', {k: v['job_id'] for k, v in jobs.items()})

    def block_of(job, needle):
        return next(b for b in job['blocks'] if needle in b['text'])

    # recorded fixture results: md decision + tentative, docx cell, pdf decision
    md = jobs['demo-project-brief.md']
    dec, tent = block_of(md, '连接池上限为 200'), block_of(md, '试运行建议')
    write_result(home, md, [
        {'candidate_id': 'CAND-md1', 'type': 'FACT', 'content': 'demo-hub 数据库连接池上限为 200（2026-08-30 会议决定，已确认）',
         'rationale': '会议记录明确记载并确认', 'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
         'evidence_spans': [{'block_id': dec['block_id'], 'quote': '连接池上限为 200'}],
         'uncertainty': {'kind': 'EXPLICIT_HEDGE', 'original_hedges': [], 'reason': '原文为明确决定'},
         'temporal': {'source_declared_date': '2026-08-30'}, 'change_intent': 'NEW'},
        {'candidate_id': 'CAND-md2', 'type': 'FACT', 'content': '连接池上限 100 仅为试运行建议，尚未正式确认',
         'rationale': '保留原文限制词', 'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
         'evidence_spans': [{'block_id': tent['block_id'], 'quote': '仅是试运行建议'}],
         'uncertainty': {'kind': 'TENTATIVE', 'original_hedges': ['仅是试运行建议'], 'reason': '试探建议'},
         'temporal': None, 'change_intent': 'NEW'},
    ], processed(md), {'model': 'recorded-fixture'})
    step('md-import', cch(home, 'ingest', 'import-result', md['job_id']))

    docx = jobs['demo-config-table.docx']
    cell = block_of(docx, '200')
    write_result(home, docx, [
        {'candidate_id': 'CAND-dx1', 'type': 'FACT', 'content': '配置表记载连接池上限当前值为 200，状态为已确认',
         'rationale': '表格单元格定位', 'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
         'evidence_spans': [{'block_id': cell['block_id'], 'quote': '200'}],
         'uncertainty': {'kind': 'EXPLICIT_HEDGE', 'original_hedges': [], 'reason': '表格明确值'},
         'temporal': {'source_declared_date': '2026-08-30'}, 'change_intent': 'NEW'},
    ], processed(docx), {'model': 'recorded-fixture'})
    step('docx-import', cch(home, 'ingest', 'import-result', docx['job_id']))

    pdf = jobs['demo-notes.pdf']
    line = block_of(pdf, 'pool limit 200')
    write_result(home, pdf, [
        {'candidate_id': 'CAND-pdf1', 'type': 'FACT', 'content': '英文笔记同样记载 pool limit 200 已确认',
         'rationale': 'PDF 文本页', 'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
         'evidence_spans': [{'block_id': line['block_id'], 'quote': 'pool limit 200'}],
         'uncertainty': {'kind': 'EXPLICIT_HEDGE', 'original_hedges': [], 'reason': '原文明确'},
         'temporal': {'source_declared_date': '2026-08-30'}, 'change_intent': 'NEW'},
    ], processed(pdf), {'model': 'recorded-fixture'})
    step('pdf-import', cch(home, 'ingest', 'import-result', pdf['job_id']))

    # Recorded, synthetic log exchange. No model or external API is called.
    # The job and fixture result are preserved for inspection.
    log = jobs['demo-worklog.log']
    user_line = block_of(log, '测试通过了一条')
    assistant_line = block_of(log, '单次成功不代表全部场景验收通过')
    write_result(home, log, [
        {'candidate_id': 'CAND-log1', 'type': 'FACT',
         'content': '2026-08-31 导入链路有一次测试通过的运行记录（仅证明当次）',
         'rationale': 'user 记录的当次运行结果，不外推为全部验收通过',
         'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
         'evidence_spans': [{'block_id': user_line['block_id'], 'quote': '2026-08-31T10:02:00Z user: 测试通过了一条，导入链路本次运行正常。'}],
         'uncertainty': {'kind': 'CONDITIONAL', 'original_hedges': ['本次运行'], 'reason': '单次成功不构成全部验收'},
         'temporal': {'source_declared_date': '2026-08-31'}, 'change_intent': 'NEW'},
        {'candidate_id': 'CAND-log2', 'type': 'FACT',
         'content': 'assistant 在同日志提示：单次成功不代表全部场景验收通过',
         'rationale': '角色为 assistant 的记录，非用户决定',
         'scope_suggestion': {'domain': 'demo', 'project': 'hub', 'topic': []},
         'evidence_spans': [{'block_id': assistant_line['block_id'], 'quote': '单次成功不代表全部场景验收通过'}],
         'uncertainty': {'kind': 'INFERRED', 'original_hedges': [], 'reason': '模型提示记录'},
         'temporal': {'source_declared_date': '2026-08-31'}, 'change_intent': 'NEW'},
    ], processed(log), {'model': 'recorded-fixture', 'request_id': 'synthetic-log-exchange'})
    step('log-import-recorded-exchange', cch(home, 'ingest', 'import-result', log['job_id']))

    for added in sources.values():
        step(f"ingest-status-{added['source_id'][:8]}", cch(home, 'ingest', 'status', added['source_id']))

    pid = cch(home, 'proposal', 'create', '--from-job', md['job_id'],
              '--candidate', 'CAND-md1', '--candidate', 'CAND-md2',
              '--id', 'PR-acc00000001', '--reason', '验收：markdown 候选')
    cch(home, 'review', 'render', 'PR-acc00000001')
    cch(home, 'review', 'approve', 'PR-acc00000001', '--reviewer', 'acceptance-runner')
    step('md-proposal-apply', cch(home, 'apply', 'PR-acc00000001'))

    # The store moved, so the old log job is stale by design: re-export the job
    # against the new revision and re-import the same candidates (blocks are
    # content-addressed, so quotes re-align identically).
    rejob = cch(home, 'ingest', 'export-job', sources['demo-worklog.log']['source_id'], '--chunk', 0)
    relog = json.loads(Path(rejob['job_path']).read_text(encoding='utf-8'))
    log_candidates = json.loads((Path(home)/'jobs'/log['job_id']/'result.json')
                                .read_text(encoding='utf-8'))['candidates']
    write_result(home, relog, log_candidates,
                 [{'block_id': b['block_id'], 'status': 'PROCESSED'} for b in relog['blocks']],
                 {'model': 'recorded-fixture',
                  'request_id': 'synthetic-log-exchange-rebase'})
    step('log-job-rebase-import', cch(home, 'ingest', 'import-result', relog['job_id']))

    pid2 = cch(home, 'proposal', 'create', '--from-job', relog['job_id'],
               '--candidate', 'CAND-log1', '--candidate', 'CAND-log2',
               '--id', 'PR-acc00000002', '--reason', '演示：虚构日志候选（recorded fixture）')
    cch(home, 'review', 'render', 'PR-acc00000002')
    cch(home, 'review', 'approve', 'PR-acc00000002', '--reviewer', 'acceptance-runner')
    step('log-proposal-apply', cch(home, 'apply', 'PR-acc00000002'))

    agent1 = cch(home, 'agent', 'context', '--domain', 'demo', '--project', 'hub',
                 '--query', '连接池上限是多少？哪些只是建议？', '--as-of', '2026-09-07')
    agent2 = cch(home, 'agent', 'context', '--domain', 'demo', '--project', 'hub',
                 '--query', '导入链路测试通过了吗？来源是什么？', '--as-of', '2026-09-07')
    assert agent1['receipt']['knowledge_revision'] == agent2['receipt']['knowledge_revision']
    ctx1 = Path(agent1['context_path']).read_text(encoding='utf-8')
    ctx2 = Path(agent2['context_path']).read_text(encoding='utf-8')
    assert '200' in ctx1 and 'TENTATIVE' in ctx1, 'agent1 context must contain the fact and preserved uncertainty'
    assert 'AI_INFERRED' in ctx2 and 'source_declared_date' not in ctx2  # provenance not over-exposed
    step('two-context-calls-same-revision', {'revision': agent1['receipt']['knowledge_revision'][:12],
                                      'run1': agent1['receipt']['run_id'], 'run2': agent2['receipt']['run_id'],
                                      'tokens1': agent1['receipt']['tokens'], 'tokens2': agent2['receipt']['tokens']})

    export_dir = out/'export'
    step('store-export', cch(home, 'store', 'export', '--to', export_dir))
    restored_home = out/'restored-home'
    step('store-restore', cch(restored_home, 'store', 'restore', '--from', export_dir))
    recompiled = cch(restored_home, 'agent', 'context', '--domain', 'demo', '--project', 'hub',
                     '--query', '连接池上限是多少？', '--as-of', '2026-09-07')
    step('restored-recompile', {'revision': recompiled['receipt']['knowledge_revision'][:12],
                                'tokens': recompiled['receipt']['tokens']})

    status = cch(home, 'store', 'status')
    report['final'] = {'history_length': status['history_length'],
                       'approved_revision': status['approved_revision'][:12],
                       'agent_runs': len(list((Path(home)/'runs').iterdir()))}
    (out/'e2e-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\nSYNTHETIC E2E DEMO PASS')
    print(json.dumps(report['final'], ensure_ascii=False))

if __name__ == '__main__':
    main()
