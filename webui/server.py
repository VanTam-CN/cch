#!/usr/bin/env python3
"""CCH 本地 Web 控制台.

单文件、仅 Python 标准库、只监听 127.0.0.1。直接调用 cch 包函数（不经 CLI
子进程），错误码与 CLI 完全一致。UI 只是协作式治理的另一个入口：所有写操作
仍然走 freeze/render/approve/apply 的既有校验，不存在绕过审批的路径。

用法（在项目根目录）:
    .venv/bin/python webui/server.py --home ./cch-home --port 8765
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cch import agent_io, ingest, proposals, publisher, reviews, sources, store  # noqa: E402
from cch.core import CCHError  # noqa: E402

MAX_BODY = 100 * 1024 * 1024
LOCK = threading.RLock()


class Ui:
    def __init__(self, home_path):
        self.home = None
        self.set_home(home_path)

    def set_home(self, path):
        root = Path(str(path)).expanduser()
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        self.home = store.Home(root)
        return str(self.home)


UI = Ui('./cch-home')


def job_dir_or_404(home, job_id):
    directory = home.jobs / job_id
    if not (directory / 'job.json').is_file():
        raise CCHError('NOT_FOUND', f'job {job_id} not found')
    return directory


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def store_view(home):
    try:
        return store.store_status(home)
    except CCHError as exc:
        if exc.code == 'NO_STORE':
            return {'state': 'UNINITIALIZED'}
        raise


def sources_view(home):
    try:
        index = sources.load_index(home)
    except Exception:
        index = {}
    result = []
    for sid, meta in sorted(index.items()):
        status = None
        if (home.extracted / sid).is_dir():
            try:
                status = ingest.status(home, sid)
            except CCHError:
                status = None
        result.append({'source_id': sid, 'name': meta.get('name'),
                       'sha256': meta.get('sha256'), 'imported_at': meta.get('imported_at'),
                       'bindings': meta.get('bindings', []), 'status': status})
    return result


def jobs_view(home):
    result = []
    if not home.jobs.exists():
        return result
    for directory in sorted(home.jobs.iterdir()):
        if not (directory / 'job.json').is_file():
            continue
        job = read_json(directory / 'job.json')
        result.append({'job_id': directory.name,
                       'source_id': job.get('source', {}).get('source_id'),
                       'source_name': job.get('source', {}).get('name'),
                       'chunk': job.get('chunk'),
                       'created_at': job.get('created_at'),
                       'blocks': len(job.get('blocks', [])),
                       'has_result': (directory / 'result.json').is_file(),
                       'has_candidates': (directory / 'candidates.json').is_file()})
    return list(reversed(result))


def runs_view(home, limit=30):
    result = []
    if not home.runs.exists():
        return result
    for directory in sorted(home.runs.iterdir(), reverse=True):
        receipt_path = directory / 'receipt.json'
        if not receipt_path.is_file():
            continue
        r = read_json(receipt_path)
        result.append({'run_id': r.get('run_id'), 'created_at': r.get('created_at'),
                       'knowledge_revision': r.get('knowledge_revision'),
                       'scope': r.get('scope'), 'tokens': r.get('tokens')})
        if len(result) >= limit:
            break
    return result


def state_payload():
    home = UI.home
    return {'home': str(home.root),
            'store': store_view(home),
            'sources': sources_view(home),
            'proposals': proposals.list_proposals(home),
            'jobs': jobs_view(home),
            'runs': runs_view(home)}


def proposal_detail(home, pid, version=None):
    version = version or proposals.latest_version(home, pid)
    envelope = proposals.read_proposal(home, pid, version)
    detail = {'proposal': envelope,
              'status': proposals.read_status(home, pid, version),
              'proposal_hash': proposals.proposal_hash(envelope),
              'approval': None, 'rejection': None,
              'bundle_text': None, 'bundle_hash': None}
    review_dir = home.reviews / pid / f'v{version}'
    for key, name in [('approval', 'approval.json'), ('rejection', 'rejection.json')]:
        path = review_dir / name
        if path.is_file():
            detail[key] = read_json(path)
    bundle = review_dir / 'bundle.md'
    if bundle.is_file():
        detail['bundle_text'] = bundle.read_text(encoding='utf-8')
        detail['bundle_hash'] = (review_dir / 'bundle.hash').read_text(encoding='utf-8').strip()
    return detail


def run_detail(home, run_id):
    directory = home.runs / run_id
    if not (directory / 'receipt.json').is_file():
        raise CCHError('NOT_FOUND', f'run {run_id} not found')
    return {'receipt': read_json(directory / 'receipt.json'),
            'context_text': (directory / 'context.md').read_text(encoding='utf-8'),
            'run_dir': str(directory)}


def do_apply(home, pid, version=None):
    version = version or proposals.latest_version(home, pid)
    envelope = proposals.read_proposal(home, pid, version)
    status = proposals.read_status(home, pid, version)
    if status['status'] == 'APPLIED':
        record = publisher.find_transaction(home, pid, version, proposals.proposal_hash(envelope))
        if record:
            return {'status': 'ALREADY_APPLIED', **{k: record[k] for k in
                    ['revision', 'transaction_id', 'proposal_id', 'proposal_version', 'proposal_hash']}}
    approval = reviews.load_approval(home, pid, version)
    bundle_hash = reviews.load_bundle_hash(home, pid, version)
    result = publisher.apply(home, envelope, approval, bundle_hash)
    if result['status'] == 'APPLIED':
        proposals.write_status(home, pid, version, 'APPLIED',
                               transaction_id=result.get('transaction_id'),
                               revision=result.get('revision'))
    return result


BLOCK_ROLE_TYPE = {'decision': 'DECISION', 'rule': 'RULE', 'term': 'TERM',
                   'preference': 'PREFERENCE', 'question': 'QUESTION', 'state': 'STATE'}


def auto_result(home, job_id):
    directory = job_dir_or_404(home, job_id)
    job = read_json(directory / 'job.json')
    candidates = []
    for index, block in enumerate(job['blocks']):
        text = block.get('text') or ''
        if len(text.strip()) < 2:
            continue
        candidates.append({
            'candidate_id': f'CAND-B{index:04d}',
            'type': BLOCK_ROLE_TYPE.get(block.get('role'), 'FACT'),
            'content': text,
            'rationale': 'webui 本地直填：内容为源文块原文，非模型改写',
            'scope_suggestion': {'domain': job['scope']['domain'],
                                 'project': job['scope']['project']},
            'evidence_spans': [{'block_id': block['block_id'], 'start': 0,
                                'end': len(text), 'quote': text}],
            'uncertainty': {'kind': 'UNKNOWN'},
            'change_intent': 'NEW'})
    result = {'schema_version': 'exchange-result/1', 'job_id': job_id,
              'manifest_hash': job['manifest_hash'],
              'blocks': [{'block_id': b['block_id'], 'status': 'PROCESSED'} for b in job['blocks']],
              'candidates': candidates,
              'model_meta': {'model': 'cch-webui-local-direct'}}
    path = directory / 'result.local.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return ingest.import_result(home, job_id, path)


def api_get(handler, path, query):
    home = UI.home
    if path == '/api/state':
        return state_payload()
    if path.startswith('/api/job/'):
        directory = job_dir_or_404(home, handler.wpath_tail)
        return {'job': read_json(directory / 'job.json'),
                'candidates': read_json(directory / 'candidates.json')
                if (directory / 'candidates.json').is_file() else None,
                'dir': str(directory),
                'files': sorted(p.name for p in directory.iterdir())}
    if path.startswith('/api/proposal/'):
        version = query.get('version', [None])[0]
        return proposal_detail(home, handler.wpath_tail, int(version) if version else None)
    if path.startswith('/api/run/'):
        return run_detail(home, handler.wpath_tail)
    if path == '/api/source':
        return {'source': sources.inspect(home, query['id'][0])}
    raise CCHError('NOT_FOUND', f'unknown GET {path}')


def api_post(handler, path, body):
    home = UI.home
    if path == '/api/home':
        return {'home': UI.set_home(body['path'])}
    if path == '/api/store/init':
        return store.store_init(home)
    if path == '/api/source/add':
        temp = None
        try:
            suffix = Path(body.get('name', 'upload.txt')).suffix
            fd, temp = tempfile.mkstemp(prefix='cch-ui-', suffix=suffix)
            with open(temp, 'wb') as f:
                f.write(bytes.fromhex(body['content_hex']))
            return sources.register(home, temp, body['domain'], body['project'],
                                    body.get('topics') or (), body.get('declared_date') or None)
        finally:
            if temp:
                Path(temp).unlink(missing_ok=True)
    if path == '/api/ingest/prepare':
        return ingest.prepare(home, body['source_id'])
    if path == '/api/ingest/export-job':
        return ingest.export_job(home, body['source_id'], int(body.get('chunk', 0)))
    if path == '/api/ingest/import-result':
        directory = job_dir_or_404(home, body['job_id'])
        result = body.get('result_path')
        if not result:
            result = directory / 'result.json'
            if not result.is_file():
                raise CCHError('NOT_FOUND',
                               '任务目录下没有 result.json；请先让 LLM 把结果保存到任务目录，'
                               '或改用本地直填按钮')
        return ingest.import_result(home, body['job_id'], result)
    if path == '/api/ingest/auto-result':
        return auto_result(home, body['job_id'])
    if path == '/api/proposal/from-job':
        return ingest.build_proposal(home, body['job_id'], body['candidate_ids'],
                                     proposal_id=body.get('proposal_id') or None,
                                     reason=body.get('reason') or None,
                                     agent_id=body.get('agent_id') or 'local-ui')
    if path == '/api/proposal/envelope':
        envelope = body['envelope']
        if isinstance(envelope, str):
            envelope = json.loads(envelope)
        return proposals.freeze(home, envelope)
    if path == '/api/proposal/refresh':
        return proposals.refresh(home, body['proposal_id'])
    if path == '/api/review/render':
        pid, version = body['proposal_id'], body.get('version')
        version = version or proposals.latest_version(home, pid)
        result = reviews.render_bundle(home, pid, version)
        result['bundle_text'] = Path(result['bundle_path']).read_text(encoding='utf-8')
        return result
    if path == '/api/review/approve':
        pid, version = body['proposal_id'], body.get('version')
        version = version or proposals.latest_version(home, pid)
        return reviews.approve(home, pid, version, body['reviewer'],
                               confirm_confidence=bool(body.get('confirm_truth')))
    if path == '/api/review/reject':
        pid, version = body['proposal_id'], body.get('version')
        version = version or proposals.latest_version(home, pid)
        return reviews.reject(home, pid, version, body['reviewer'], body['reason'])
    if path == '/api/apply':
        return do_apply(home, body['proposal_id'], body.get('version'))
    if path == '/api/agent/context':
        from cch.core import instant
        result = agent_io.agent_context(
            home, body['query'], body['domain'], body['project'], body.get('topics') or (),
            as_of=instant(body['as_of']) if body.get('as_of') else None,
            budget=int(body.get('budget', 16000)), evidence=body.get('evidence', 'auto'),
            revision=body.get('revision') or None)
        result['context_text'] = Path(result['context_path']).read_text(encoding='utf-8')
        return result
    if path == '/api/agent/check':
        target = Path(body['receipt'])
        if not target.is_file() and (home.runs / body['receipt'] / 'receipt.json').is_file():
            target = home.runs / body['receipt'] / 'receipt.json'
        return agent_io.agent_check(home, target, body.get('domain'), body.get('project'))
    raise CCHError('NOT_FOUND', f'unknown POST {path}')


class Handler(BaseHTTPRequestHandler):
    server_version = 'CCH-WebUI/0.1'

    def log_message(self, fmt, *args):
        sys.stderr.write('[webui] ' + fmt % args + '\n')

    def _send(self, code, payload, content_type='application/json; charset=utf-8'):
        data = payload if isinstance(payload, bytes) else \
            json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc):
        if isinstance(exc, CCHError):
            self._send(400, {'error': {'code': exc.code, 'message': exc.message}})
        else:
            traceback.print_exc()
            self._send(500, {'error': {'code': 'INTERNAL', 'message': str(exc)}})

    def do_GET(self):
        parsed = urlparse(self.path)
        self.wpath_tail = parsed.path.rstrip('/').rsplit('/', 1)[-1]
        try:
            with LOCK:
                if parsed.path in ('/', '/index.html'):
                    return self._send(200, PAGE.encode('utf-8'), 'text/html; charset=utf-8')
                if parsed.path == '/favicon.ico':
                    return self._send(204, b'', 'image/x-icon')
                result = api_get(self, parsed.path, parse_qs(parsed.query))
            return self._send(200, result)
        except Exception as exc:
            return self._error(exc)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > MAX_BODY:
                raise CCHError('VALIDATION_ERROR', '请求体超过 100MB 上限')
            raw = self.rfile.read(length) if length else b'{}'
            body = json.loads(raw.decode('utf-8')) if raw.strip() else {}
            if not isinstance(body, dict):
                raise CCHError('VALIDATION_ERROR', '请求体必须是 JSON 对象')
            with LOCK:
                result = api_post(self, parsed.path, body)
            return self._send(200, result)
        except Exception as exc:
            return self._error(exc)


def main():
    parser = argparse.ArgumentParser(description='CCH 本地 Web 控制台（127.0.0.1 专用）')
    parser.add_argument('--home', default='./cch-home', help='cch-home 目录（页面内可切换）')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    UI.set_home(args.home)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'CCH Web UI: http://127.0.0.1:{args.port}/  (home: {UI.home.root})')
    print('仅监听本机回环地址；关闭终端即停止服务。Ctrl+C 退出。')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- 前端页面

PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CCH 认知库控制台</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='0.9em' font-size='90'%3E%F0%9F%A7%A0%3C/text%3E%3C/svg%3E">
<style>
:root{
  --bg:#f4f2ed; --panel:#ffffff; --ink:#1f2933; --muted:#6b7280;
  --accent:#0f766e; --accent2:#f59e0b; --line:#e5e7eb;
  --ok:#047857; --warn:#b45309; --bad:#b91c1c;
}
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--ink)}
header{background:linear-gradient(135deg,#0f766e,#115e59);color:#fff;padding:14px 22px;
  display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:18px;margin:0;font-weight:600}
header .home{font-size:12px;opacity:.85;font-family:ui-monospace,monospace}
header input{flex:1;min-width:220px;max-width:420px;padding:6px 10px;border:none;border-radius:6px;
  font:13px ui-monospace,monospace}
header button{padding:6px 14px;border:none;border-radius:6px;background:rgba(255,255,255,.18);
  color:#fff;cursor:pointer;font-size:13px}
header button:hover{background:rgba(255,255,255,.3)}
nav{display:flex;gap:2px;background:var(--panel);border-bottom:1px solid var(--line);
  padding:0 14px;overflow-x:auto;position:sticky;top:0;z-index:5}
nav button{border:none;background:none;padding:12px 16px;font-size:14px;cursor:pointer;
  color:var(--muted);border-bottom:2px solid transparent;white-space:nowrap}
nav button.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
main{max-width:1060px;margin:0 auto;padding:20px 16px 60px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card h2{margin:0 0 12px;font-size:16px;color:var(--ink)}
.card h2 small{font-weight:normal;color:var(--muted);margin-left:8px;font-size:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.row > *{min-width:0}
label.f{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
label.f input,label.f select,label.f textarea{padding:7px 10px;border:1px solid var(--line);
  border-radius:6px;font:inherit;font-size:14px;background:#fff}
label.f textarea{font-family:ui-monospace,monospace;font-size:13px}
label.grow{flex:1;min-width:180px}
button.act{background:var(--accent);color:#fff;border:none;border-radius:6px;
  padding:8px 16px;font-size:14px;cursor:pointer}
button.act:hover{background:#0d5f59}
button.act.warn{background:var(--accent2)}
button.act.warn:hover{background:#d97706}
button.act.bad{background:var(--bad)}
button.act:disabled{opacity:.5;cursor:not-allowed}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px;white-space:nowrap}
code,.mono{font-family:ui-monospace,monospace;font-size:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.tag.ok{background:#d1fae5;color:var(--ok)}
.tag.warn{background:#fef3c7;color:var(--warn)}
.tag.bad{background:#fee2e2;color:var(--bad)}
.tag.gray{background:#e5e7eb;color:#4b5563}
pre.out{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:14px;
  overflow:auto;max-height:420px;font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-all}
pre.out.md{background:#fffbeb;color:#78350f}
.hint{font-size:12px;color:var(--muted);margin-top:6px}
.divider{height:1px;background:var(--line);margin:14px 0}
.flow{display:flex;gap:6px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-bottom:4px}
.flow b{color:var(--accent)}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.chip{font-size:12px;padding:2px 10px;border:1px solid var(--line);border-radius:12px;cursor:pointer}
.chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.kv{display:grid;grid-template-columns:130px 1fr;gap:4px 12px;font-size:13px}
.kv dt{color:var(--muted)}
.kv dd{margin:0;word-break:break-all}
</style>
</head>
<body>
<header>
  <h1>📚 CCH 认知库控制台</h1>
  <input id="homePath" class="mono" placeholder="/path/to/cch-home">
  <button onclick="switchHome()">切换/新建认知库</button>
  <span class="home mono" id="homeLabel"></span>
  <span id="storeBadge"></span>
  <span style="flex:1"></span>
  <span style="font-size:12px;opacity:.75">127.0.0.1 · 协作式治理 · 审批不可绕过</span>
</header>
<nav id="nav">
  <button data-v="dashboard" class="on">总览</button>
  <button data-v="sources">① 资料</button>
  <button data-v="ingest">② 提炼</button>
  <button data-v="proposals">③ 提案与审批</button>
  <button data-v="bootstrap">④ 首次发布</button>
  <button data-v="query">⑤ 知识查询</button>
  <button data-v="runs">运行记录</button>
</nav>
<main id="main"></main>

<script>
let S = null;          // /api/state 快照
let view = 'dashboard';
let sel = {job:null, proposal:null, candidates:new Set(), run:null};
const $ = id => document.getElementById(id);

async function api(path, body){
  const opt = body !== undefined
    ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}
    : {};
  const res = await fetch(path, opt);
  const data = await res.json();
  if(!res.ok){ throw new Error((data.error && data.error.code || res.status) + ': ' + (data.error && data.error.message || '')); }
  return data;
}

function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function pre(obj, cls){ return '<pre class="out '+(cls||'')+'">'+esc(typeof obj==='string'?obj:JSON.stringify(obj,null,2))+'</pre>'; }
function badge(store){
  const map = {OK:['ok','已批准 r'+ (store.approved_revision||'').slice(0,7)],
               EMPTY:['warn','空库（待首次发布）'],
               UNINITIALIZED:['bad','未初始化']};
  const [c,t] = map[store.state]||['gray',store.state];
  return '<span class="tag '+c+'">'+t+'</span>';
}
function ptag(st){
  const map = {SUBMITTED:'gray',READY:'ok',APPROVED:'ok',APPLIED:'ok',
               NEEDS_FIX:'warn',STALE:'warn',REJECTED:'bad'};
  return '<span class="tag '+(map[st]||'gray')+'">'+esc(st)+'</span>';
}
function toast(msg, err){
  const el = document.createElement('div');
  el.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);'+
    'background:'+(err?'#b91c1c':'#0f766e')+';color:#fff;padding:10px 22px;border-radius:8px;'+
    'font-size:14px;z-index:99;box-shadow:0 4px 12px rgba(0,0,0,.25);max-width:80vw';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(()=>el.remove(), err?6000:3000);
}
async function act(fn, okMsg){
  try{ const r = await fn(); if(okMsg) toast(okMsg); await refresh(); return r; }
  catch(e){ toast(e.message, true); throw e; }
}

async function refresh(){ S = await api('/api/state'); render(); }
async function switchHome(){
  const p = $('homePath').value.trim();
  if(!p) return toast('请输入路径', true);
  await act(()=>api('/api/home',{path:p}), '已切换');
}
$('nav').onclick = e => {
  const b = e.target.closest('button'); if(!b) return;
  view = b.dataset.v;
  document.querySelectorAll('#nav button').forEach(x=>x.classList.toggle('on', x===b));
  sel = {job:null, proposal:null, run:null, candidates:new Set()};
  render();
};

/* ---------------- 总览 ---------------- */
function vDashboard(){
  const st = S.store, ap = S.proposals.filter(p=>['SUBMITTED','READY','APPROVED','STALE','NEEDS_FIX'].includes(p.status));
  return '<div class="card"><h2>库状态 '+badge(st)+'</h2><dl class="kv">'
    +'<dt>认知库目录</dt><dd class="mono">'+esc(S.home)+'</dd>'
    +(st.approved_revision?'<dt>已批准版本</dt><dd class="mono">'+esc(st.approved_revision)+'</dd>'
      +'<dt>历史长度</dt><dd>'+esc(st.history_length)+' 次发布</dd>'
      +'<dt>对象数</dt><dd>'+esc(st.tree_files||'-')+'</dd>':'')
    +'<dt>隔离模式</dt><dd>'+esc(st.mode||'cooperative')+' — '+(st.enforced?'OS 级强制':'协作式（工具级约束，非 OS 强制）')+'</dd></dl>'
    +'<div class="hint">协作式模式说明：所有程序运行在同一 OS 用户下，治理依赖流程与工具约束，不是操作系统级的"仅人类可写"保证。</div></div>'
    +'<div class="card"><h2>完整链路</h2><div class="flow">'
    +'<b>① 导入资料</b> → <b>② 导出提炼任务</b> → 手工/LLM 填结果 → <b>③ 导入候选 → 建提案</b>'
    +' → <b>渲染审批单</b> → <b>人工批准</b> → <b>发布</b> → <b>知识查询</b></div>'
    +'<div class="hint">审批人记录的是名字，不是身份认证（协作式模式）。发布采用 CAS：并发发布恰有一个成功。</div></div>'
    +'<div class="card"><h2>待处理提案 <small>'+ap.length+' 个</small></h2>'
    +(ap.length?'<table><tr><th>提案</th><th>状态</th><th>来源</th><th>版本</th></tr>'
      +ap.map(p=>'<tr><td><a href="#" onclick="gotoProposal(\''+esc(p.proposal_id)+'\');return false" class="mono">'+esc(p.proposal_id)+'</a></td>'
        +'<td>'+ptag(p.status)+'</td><td>'+esc(p.origin||'-')+'</td><td>v'+p.latest_version+'</td></tr>').join('')+'</table>'
      :'<div class="empty">暂无待处理提案</div>')+'</div>'
    +'<div class="card"><h2>最近运行</h2>'+(S.runs.length
      ?'<table><tr><th>时间</th><th>范围</th><th>知识版本</th><th>Token</th></tr>'
      +S.runs.slice(0,6).map(r=>'<tr><td class="mono">'+esc(r.created_at)+'</td>'
        +'<td>'+esc(r.scope? r.scope.domain+'/'+r.scope.project :'-')+'</td>'
        +'<td class="mono">'+esc((r.knowledge_revision||'').slice(0,7))+'</td>'
        +'<td>'+(r.tokens? r.tokens.total:'-')+'</td></tr>').join('')+'</table>'
      :'<div class="empty">还没有查询记录</div>')+'</div>';
}

/* ---------------- ① 资料 ---------------- */
function vSources(){
  return '<div class="card"><h2>导入资料</h2>'
    +'<div class="row">'
    +'<label class="f"><input type="file" id="srcFile"></label>'
    +'<label class="f grow">域 *<input id="srcDomain" placeholder="例如 power"></label>'
    +'<label class="f grow">项目 *<input id="srcProject" placeholder="例如 grid-a"></label>'
    +'<label class="f grow">主题（逗号分隔）<input id="srcTopics" placeholder="例如 运维,协议"></label>'
    +'<label class="f">事实日期<input id="srcDate" type="date"></label>'
    +'<button class="act" onclick="addSource()">导入</button></div>'
    +'<div class="hint">支持 md / txt / docx / pdf / log / jsonl 等。域和项目是必填的显式范围，主题可选。</div></div>'
    +'<div class="card"><h2>已导入 <small>'+S.sources.length+' 份</small></h2>'
    +(S.sources.length?'<table><tr><th>名称</th><th>域/项目</th><th>导入时间</th><th>提炼状态</th><th></th></tr>'
      +S.sources.map(s=>{
        const b = s.bindings[0]||{};
        const st = s.status;
        return '<tr><td class="mono">'+esc(s.name)+'</td>'
        +'<td>'+esc(b.domain||'')+' / '+esc(b.project||'')+'</td>'
        +'<td class="mono" style="font-size:11px">'+esc(s.imported_at||'')+'</td>'
        +'<td>'+(st? '<span class="tag '+(st.blocks_processed===st.blocks_pending&&st.blocks_pending===0?'gray':'warn')+'">'
          +esc(st.coverage||st.state)+'</span>' : '<span class="tag gray">未提炼</span>')+'</td>'
        +'<td><button class="act" style="padding:4px 10px;font-size:12px" onclick="prepSource(\''+esc(s.source_id)+'\')">去提炼</button></td></tr>';
      }).join('')+'</table>'
      :'<div class="empty">还没有资料。先在上面导入一份。</div>')+'</div>';
}

async function addSource(){
  const f = $('srcFile').files[0];
  const domain = $('srcDomain').value.trim(), project = $('srcProject').value.trim();
  if(!f || !domain || !project) return toast('请选择文件并填写域和项目', true);
  const buf = await f.arrayBuffer();
  const hex = [...new Uint8Array(buf)].map(b=>b.toString(16).padStart(2,'0')).join('');
  const topics = $('srcTopics').value.split(/[,，]/).map(t=>t.trim()).filter(Boolean);
  await act(()=>api('/api/source/add',{name:f.name, content_hex:hex, domain, project,
    topics, declared_date:$('srcDate').value||null}), '已导入');
}
async function prepSource(sid){
  $('nav').querySelector('[data-v=ingest]').click();
  await act(()=>api('/api/ingest/prepare',{source_id:sid}), '已切片');
  const st = (await api('/api/state')).sources.find(x=>x.source_id===sid);
  if(st && st.status && st.status.chunks) sel.prepSource = sid;
  render();
}

/* ---------------- ② 提炼 ---------------- */
function vIngest(){
  const ready = S.sources.filter(s=>s.status && s.status.chunks>0);
  let html = '<div class="card"><h2>导出提炼任务</h2>';
  if(!ready.length) return html+'<div class="empty">还没有已切片的资料。先到 ① 资料页导入并切片。</div></div>'
    + jobsTable();
  html += '<div class="row">'
    +'<label class="f grow">资料<select id="jobSrc">'
    +ready.map(s=>'<option value="'+esc(s.source_id)+'">'+esc(s.name)+'（'+s.status.chunks+' 块 / '+s.status.blocks+' 条内容块）</option>').join('')
    +'</select></label>'
    +'<label class="f" style="width:90px">分块序号<input id="jobChunk" type="number" value="0" min="0"></label>'
    +'<button class="act" onclick="exportJob()">导出任务</button></div>'
    +'<div class="hint">大文件自动分块；每块生成一个独立任务（job）。先处理第 0 块。</div></div>';
  return html + jobsTable();
}

function jobsTable(){
  if(!S.jobs.length) return '<div class="card"><h2>任务列表</h2><div class="empty">暂无任务</div></div>';
  return '<div class="card"><h2>任务列表 <small>'+S.jobs.length+' 个</small></h2><table>'
    +'<tr><th>任务</th><th>资料</th><th>块数</th><th>结果</th><th>候选</th><th>操作</th></tr>'
    +S.jobs.map(j=>'<tr'+(sel.job===j.job_id?' style="background:#f0fdfa"':'')+'><td class="mono">'+esc(j.job_id)+'</td>'
      +'<td>'+esc(j.source_name||'')+(j.chunk>0?' <span class="tag gray">块'+j.chunk+'</span>':'')+'</td>'
      +'<td>'+j.blocks+'</td>'
      +'<td>'+(j.has_candidates?'<span class="tag ok">已导入</span>':j.has_result?'<span class="tag warn">待导入</span>':'<span class="tag gray">未填写</span>')+'</td>'
      +'<td>'+(j.has_candidates?'<a href="#" onclick="openJob(\''+esc(j.job_id)+'\');return false">查看</a>':'-')+'</td>'
      +'<td style="white-space:nowrap">'
      +(!j.has_result&&!j.has_candidates?'<button class="act warn" style="padding:4px 10px;font-size:12px" onclick="autoFill(\''+esc(j.job_id)+'\')">本地直填</button>':'')
      +(!j.has_candidates?'<button class="act" style="padding:4px 10px;font-size:12px" onclick="importResult(\''+esc(j.job_id)+'\')">导入 result.json</button>':'')
      +(j.has_candidates?'<button class="act" style="padding:4px 10px;font-size:12px" onclick="openJob(\''+esc(j.job_id)+'\')">选择候选建提案</button>':'')
      +'</td></tr>').join('')+'</table>'
    +'<div class="hint">外部 LLM 流程：任务目录里有 job.json，把它交给 LLM，让它把结果存回同目录 result.json，然后点"导入"。<br>'
    +'本地直填：无 LLM 时的捷径——每个内容块原样成为候选（类型按声明角色、不确定性=UNKNOWN），后续人工审批把关。</div></div>';
}

async function exportJob(){
  await act(()=>api('/api/ingest/export-job',{source_id:$('jobSrc').value, chunk:+$('jobChunk').value||0}),'任务已导出');
}
async function autoFill(jid){
  await act(()=>api('/api/ingest/auto-result',{job_id:jid}),'本地直填完成');
  openJob(jid);
}
async function importResult(jid){
  await act(()=>api('/api/ingest/import-result',{job_id:jid}),'结果已导入');
  openJob(jid);
}
async function openJob(jid){
  sel.job = jid;
  sel.candidates = new Set();
  const d = await api('/api/job/'+jid);
  const el = $('main').querySelector('[data-job-panel]');
  if(el) el.innerHTML = jobPanel(d);
  else { $('main').insertAdjacentHTML('beforeend','<div data-job-panel>'+jobPanel(d)+'</div>'); }
  el && el.scrollIntoView({behavior:'smooth'});
}

function jobPanel(d){
  const cands = (d.candidates&&d.candidates.candidates)||[];
  const bySt = {};
  cands.forEach(c=>bySt[c.status]=(bySt[c.status]||0)+1);
  let html = '<div class="card" style="border-color:#0f766e"><h2>任务 '+esc(d.job.job_id)+' <small>'
    +esc(d.job.source.name)+' · '+(d.job.chunk||0)+' 块</small></h2>';
  html += '<div class="chips">'+cands.map(c=>'<span class="tag '
    +(c.status==='VALIDATED'?'ok':c.status==='REJECTED_BY_INTENT'||c.status==='DUPLICATE_OF'?'gray':'warn')+'">'
    +esc(c.candidate_id)+' '+esc(c.status)+(c.duplicate_of?' →'+esc(c.duplicate_of):'')+'</span>').join('')+'</div>';
  const usable = cands.filter(c=>c.status==='VALIDATED' && c.change_intent==='NEW');
  html += '<div class="divider"></div><div class="row">'
    +'<label class="f grow">提案编号（可空自动生成）<input id="propId" placeholder="PR-xxx"></label>'
    +'<label class="f grow">提案理由<input id="propReason" placeholder="例如：从运维日志沉淀决策"></label>'
    +'<button class="act" '+(usable.length?'':'disabled')+' onclick="buildProposal()">'
    +'用选中候选建提案（'+usable.length+' 条可用）</button>'
    +'<button class="act warn" onclick="selAllUsable('+(usable.length?1:0)+')">全选</button></div>';
  if(!usable.length) html += '<div class="hint">没有 VALIDATED+NEW 的候选：去重/证据对齐未通过的候选需要手工信封处理（见 ④）。</div>';
  html += '<div class="divider"></div><div class="chips" id="candChips">';
  usable.forEach(c=>{ html += '<span class="chip'+(sel.candidates.has(c.candidate_id)?' on':'')+'" data-cid="'+esc(c.candidate_id)+'"'
    +' onclick="toggleCand(this)">'+esc(c.candidate_id)+' · '+esc(c.type)+'</span>'; });
  html += '</div>';
  if(sel.candidates.size){
    html += '<div style="margin-top:8px">';
    cands.filter(c=>sel.candidates.has(c.candidate_id)).forEach(c=>{
      html += '<div style="margin-bottom:10px"><b class="mono" style="font-size:12px">'+esc(c.candidate_id)+'</b>'
        +'<pre class="out md" style="max-height:160px">'+esc(c.content)+'</pre></div>';
    });
    html += '</div>';
    }
  return html+'</div>';
}

function toggleCand(el){ el.classList.toggle('on');
  const cid = el.dataset.cid;
  el.classList.contains('on') ? sel.candidates.add(cid) : sel.candidates.delete(cid);
}
function selAllUsable(){ document.querySelectorAll('#candChips .chip').forEach(c=>{ if(!c.classList.contains('on')) c.click(); }); }

async function buildProposal(){
  if(!sel.job || !sel.candidates.size) return toast('先选候选', true);
  await act(()=>api('/api/proposal/from-job',{job_id:sel.job, candidate_ids:[...sel.candidates],
    proposal_id:$('propId').value.trim()||null, reason:$('propReason').value.trim()||null}),'提案已创建');
  sel.job=null; sel.candidates=new Set();
  document.querySelector('[data-job-panel]')?.remove();
  $('nav').querySelector('[data-v=proposals]').click();
}

/* ---------------- ③ 提案与审批 ---------------- */
function vProposals(){
  let html = '<div class="card"><h2>提案列表</h2>';
  html += S.proposals.length?'<table><tr><th>提案</th><th>状态</th><th>来源</th><th>理由</th><th></th></tr>'
    +S.proposals.map(p=>'<tr'+(sel.proposal===p.proposal_id?' style="background:#f0fdfa"':'')+'><td class="mono">'+esc(p.proposal_id)+'</td>'
      +'<td>'+ptag(p.status)+'</td><td>'+esc(p.origin||'-')+'</td>'
      +'<td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(p.reason||'')+'</td>'
      +'<td><button class="act" style="padding:4px 10px;font-size:12px" onclick="openProposal(\''+esc(p.proposal_id)+'\')">打开</button></td></tr>').join('')+'</table>'
    :'<div class="empty">暂无提案。在 ② 里从任务建提案，或到 ④ 用信封首发。</div>';
  html += '</div><div id="propPanel"></div>';
  return html;
}

async function openProposal(pid){
  sel.proposal = pid;
  const d = await api('/api/proposal/'+pid);
  $('propPanel').innerHTML = propPanel(d);
  $('propPanel').scrollIntoView({behavior:'smooth'});
}
function gotoProposal(pid){
  view='proposals';
  document.querySelectorAll('#nav button').forEach(x=>x.classList.toggle('on', x.dataset.v==='proposals'));
  $('main').innerHTML = vProposals();
  openProposal(pid);
}

function propPanel(d){
  const p = d.proposal, st = d.status.status;
  let html = '<div class="card" style="border-color:#0f766e"><h2>'+esc(p.proposal_id)+' v'+p.proposal_version
    +' '+ptag(st)+' <small class="mono">hash '+esc(d.proposal_hash.slice(0,12))+'… · base '+esc(p.base_revision.slice(0,7))+'</small></h2>';
  html += '<dl class="kv"><dt>范围</dt><dd>'+esc(p.scope.domain)+' / '+esc(p.scope.project)
    +(p.scope.topic.length?' / '+esc(p.scope.topic.join(', ')):'')+'</dd>'
    +'<dt>理由</dt><dd>'+esc(p.reason)+'</dd>'
    +'<dt>提交者</dt><dd class="mono">'+esc(p.submitter.agent_id)+'</dd>'
    +'<dt>操作数</dt><dd>'+p.operations.length+'（'+p.operations.map(o=>o.op).join(', ')+'）</dd>'
    +'<dt>风险标记</dt><dd>'+((p.risk_flags||[]).map(f=>'<span class="tag warn">'+esc(f)+'</span>').join(' ')||'-')+'</dd></dl>';
  if(d.approval) html += '<div class="hint" style="color:var(--ok)">✓ 已批准 by '+esc(d.approval.reviewer)
    +' at '+esc(d.approval.approved_at)+'（bundle '+esc(d.approval.review_bundle_hash.slice(0,12))+'…）</div>';
  if(d.rejection) html += '<div class="hint" style="color:var(--bad)">✗ 已否决：'+esc(d.rejection.reason)+'</div>';
  if(d.bundle_text) html += '<div class="divider"></div><h2 style="font-size:14px">审批单（bundle）</h2>'
    +pre(d.bundle_text,'md');
  html += '<div class="divider"></div>';
  // 操作按钮按状态
  if(st==='SUBMITTED'||st==='NEEDS_FIX'||st==='STALE'){
    html += '<div class="row"><button class="act" onclick="renderBundle()">渲染审批单</button>'
      +(st==='STALE'?'<button class="act warn" onclick="refreshProposal()">对齐最新已批准版本（refresh）</button>':'')
      +'</div><div class="hint">渲染会生成 bundle.md + 哈希；审批绑定这份哈希。STALE 说明基线已前进，先 refresh。</div>';
  }
  if(st==='READY'){
    html += '<div class="row">'
      +'<label class="f grow">审批人（记录用，非认证）<input id="revName" placeholder="你的名字"></label>'
      +'<label class="f" style="flex-direction:row;align-items:center;gap:6px;padding-top:18px">'
      +'<input type="checkbox" id="confTruth"> 我已另行核实内容真实性</label>'
      +'<button class="act" onclick="doApprove()">批准</button>'
      +'<button class="act bad" onclick="doReject()">否决</button></div>'
      +'<div class="hint">批准绑定 bundle 哈希与提案哈希。若渲染后提案被改动（哈希变），批准会失效——这正是防篡改设计。</div>';
  }
  if(st==='APPROVED'){
    html += '<div class="row"><button class="act" onclick="doApply()">🚀 发布（apply）</button></div>'
      +'<div class="hint">发布是 CAS 操作：多个并发发布只有一个成功，其余报 CAS_CONFLICT。</div>';
  }
  if(st==='APPLIED') html += '<div class="hint" style="color:var(--ok)">✓ 已发布，进入知识库（可到 ⑤ 查询）</div>';
  html += '<div class="divider"></div><details><summary style="cursor:pointer;font-size:13px;color:var(--muted)">查看完整信封 JSON</summary>'
    +pre(p)+'</details>';
  return html+'</div>';
}

async function renderBundle(){
  const r = await act(()=>api('/api/review/render',{proposal_id:sel.proposal}),'审批单已渲染');
  openProposal(sel.proposal);
}
async function doApprove(){
  const name = $('revName').value.trim();
  if(!name) return toast('填审批人', true);
  await act(()=>api('/api/review/approve',{proposal_id:sel.proposal, reviewer:name,
    confirm_truth:$('confTruth').checked}),'已批准，可以去发布');
}
async function doReject(){
  const name = $('revName').value.trim(), reason = prompt('否决理由（必填）:');
  if(!name || !reason) return;
  await act(()=>api('/api/review/reject',{proposal_id:sel.proposal, reviewer:name, reason}),'已否决');
}
async function refreshProposal(){
  await act(()=>api('/api/proposal/refresh',{proposal_id:sel.proposal}),'已对齐新基线（生成新版本）');
}
async function doApply(){
  await act(()=>api('/api/apply',{proposal_id:sel.proposal}),'🎉 已发布到知识库');
  openProposal(sel.proposal);
}

/* ---------------- ④ 首次发布（手工信封） ---------------- */
function vBootstrap(){
  const empty = S.store.state==='EMPTY'||S.store.state==='UNINITIALIZED';
  let html = '<div class="card"><h2>首次发布 <small>空库 → 第一批知识</small></h2>';
  if(!empty && S.store.state==='OK'){
    return html+'<div class="hint" style="color:var(--ok)">库已有已批准版本，日常提案走 ②③ 即可。</div></div>';
  }
  html += (S.store.state==='UNINITIALIZED'
    ?'<div class="row"><button class="act" onclick="initStore()">初始化库（store init）</button></div>'
    +'<div class="hint">首次发布前必须先初始化（创建 store.git）。空库首发必须用"手工信封"——这是 CLI 的既有约束，提炼链路需要库里有已批准版本才能导出任务。</div>'
    :'<div class="hint">空库首发：填一个对象（信封只支持一个 op 时最简单），发布后即可使用日常链路 ②③。</div>')
    +'<div class="divider"></div>'
    +'<label class="f" style="margin-bottom:8px">信封 JSON（模板已按 proposal/1 预填，改对象内容即可）'
    +'<textarea id="envText" rows="22" style="min-height:380px"></textarea></label>'
    +'<div class="row"><button class="act" onclick="fillEnvelope()">载入模板</button>'
    +'<button class="act" onclick="submitEnvelope()">校验并冻结提案</button></div>'
    +'<div class="hint">提交后提案状态 SUBMITTED，到 ③ 渲染审批单 → 批准 → 发布。</div></div>';
  return html;
}

function envelopeTemplate(){
  const now = new Date().toISOString().slice(0,19)+'Z';
  return JSON.stringify({
    schema_version:'proposal/1',
    proposal_id:'PR-bootstrap-'+Date.now().toString(36),
    proposal_version:1,
    base_revision:'0'.repeat(40),
    scope:{domain:'demo',project:'first',topic:[]},
    submitter:{agent_id:'local-ui'},
    origin:{kind:'manual'},
    reason:'首次发布（bootstrap）',
    operations:[{op:'create', object_id:'FACT-hello-0001',
      expected_object_hash:null,
      after:{id:'FACT-hello-0001', type:'FACT', status:'ACTIVE',
        scope:{domain:'demo',project:'first',topic:[]},
        confidence:'CONFIRMED', authority:'USER_CONFIRMED',
        priority:'REFERENCE',
        content:'这里写第一条知识内容',
        rationale:'首发种子知识',
        relations:{supersedes:[],depends_on:[],conflicts_with:[],derived_from:[]},
        provenance:{evidence_refs:[]},
        lifecycle:{valid_from:now, valid_until:null, ttl_hours:null},
        retrieval:{keywords:[],aliases:[]},
        tags:['bootstrap']}},
    ],
    evidence_assets:[], candidate_assets:[], risk_flags:['BOOTSTRAP'],
    requested_at:now}, null, 2);
}
function fillEnvelope(){ $('envText').value = envelopeTemplate(); }
async function submitEnvelope(){
  try{ JSON.parse($('envText').value); }catch(e){ return toast('JSON 语法错误: '+e.message, true); }
  await act(()=>api('/api/proposal/envelope',{envelope:$('envText').value}),'信封已冻结为提案');
  $('nav').querySelector('[data-v=proposals]').click();
  const ps = (await api('/api/state')).proposals;
  if(ps.length) openProposal(ps[0].proposal_id);
}
async function initStore(){ await act(()=>api('/api/store/init',{}),'已初始化'); }

/* ---------------- ⑤ 知识查询 ---------------- */
function vQuery(){
  if(S.store.state!=='OK') return '<div class="card"><h2>知识查询</h2><div class="empty">库还没有已批准版本，先完成首次发布。</div></div>';
  return '<div class="card"><h2>agent context — 编译上下文</h2>'
    +'<div class="row">'
    +'<label class="f grow">任务描述 *<input id="qQuery" placeholder="例如：总结这个项目的当前决策"></label>'
    +'<label class="f" style="width:110px">域 *<input id="qDomain"></label>'
    +'<label class="f" style="width:110px">项目 *<input id="qProject"></label>'
    +'<label class="f" style="width:130px">主题<input id="qTopics" placeholder="逗号分隔"></label></div>'
    +'<div class="row" style="margin-top:10px">'
    +'<label class="f" style="width:110px">预算(token)<input id="qBudget" type="number" value="16000"></label>'
    +'<label class="f" style="width:150px">证据模式<select id="qEvidence">'
    +'<option>auto</option><option>none</option><option>summary</option><option>full</option></select></label>'
    +'<label class="f" style="width:170px">历史版本（可空）<input id="qRevision" placeholder="commit 哈希"></label>'
    +'<label class="f" style="width:170px">as-of（可空）<input id="qAsOf" placeholder="ISO 时间"></label>'
    +'<button class="act" onclick="runQuery()">编译上下文</button></div></div>'
    +'<div id="qResult"></div>'
    +'<div class="card"><h2>运行记录 <small>'+S.runs.length+'</small></h2>'
    +(S.runs.length?'<table><tr><th>时间</th><th>范围</th><th>版本</th><th>Token</th><th></th></tr>'
      +S.runs.map(r=>'<tr><td class="mono" style="font-size:11px">'+esc(r.created_at)+'</td>'
        +'<td>'+esc(r.scope? r.scope.domain+'/'+r.scope.project:'-')+'</td>'
        +'<td class="mono">'+esc((r.knowledge_revision||'').slice(0,7))+'</td>'
        +'<td>'+(r.tokens? r.tokens.total:'-')+'</td>'
        +'<td><button class="act" style="padding:3px 9px;font-size:11px" onclick="openRun(\''+esc(r.run_id)+'\')">详情</button></td></tr>').join('')+'</table>'
      :'<div class="empty">暂无</div>')+'</div>';
}

async function runQuery(){
  const body = {query:$('qQuery').value.trim(), domain:$('qDomain').value.trim(),
    project:$('qProject').value.trim(), topics:$('qTopics').value.split(/[,，]/).map(t=>t.trim()).filter(Boolean),
    budget:+$('qBudget').value||16000, evidence:$('qEvidence').value};
  if($('qRevision').value.trim()) body.revision = $('qRevision').value.trim();
  if($('qAsOf').value.trim()) body.as_of = $('qAsOf').value.trim();
  if(!body.query||!body.domain||!body.project) return toast('任务描述、域、项目必填', true);
  const r = await act(()=>api('/api/agent/context',body),'编译完成');
  $('qResult').innerHTML = '<div class="card"><h2>编译结果 <small class="mono">'
    +esc(r.receipt.run_id)+'</small></h2>'
    +'<dl class="kv"><dt>知识版本</dt><dd class="mono">'+esc(r.receipt.knowledge_revision)+'</dd>'
    +'<dt>Token</dt><dd>'+JSON.stringify(r.receipt.tokens)+'</dd>'
    +'<dt>证据模式</dt><dd>'+esc(r.receipt.evidence_mode)+'</dd></dl>'
    +'<div class="divider"></div>'+pre(r.context_text,'md')+'</div>';
}
async function openRun(rid){
  const d = await api('/api/run/'+rid);
  $('qResult').innerHTML = '<div class="card"><h2>运行 '+esc(d.receipt.run_id)+'</h2>'
    +'<dl class="kv"><dt>知识版本</dt><dd class="mono">'+esc(d.receipt.knowledge_revision)+'</dd>'
    +'<dt>context 哈希</dt><dd class="mono">'+esc(d.receipt.context_hash)+'</dd>'
    +'<dt>编译器哈希</dt><dd class="mono">'+esc(d.receipt.compiler_hash)+'</dd></dl>'
    +'<div class="divider"></div>'+pre(d.context_text,'md')+'</div>';
}

/* ---------------- 渲染分发 ---------------- */
const VIEWS = {dashboard:vDashboard, sources:vSources, ingest:vIngest,
               proposals:vProposals, bootstrap:vBootstrap, query:vQuery,
               runs:()=> '<div class="card"><h2>全部运行记录</h2>'+(S.runs.length
                 ?'<table><tr><th>时间</th><th>范围</th><th>知识版本</th><th>Token</th></tr>'
                 +S.runs.map(r=>'<tr><td class="mono" style="font-size:11px">'+esc(r.created_at)+'</td>'
                   +'<td>'+esc(r.scope? r.scope.domain+'/'+r.scope.project:'-')+'</td>'
                   +'<td class="mono">'+esc((r.knowledge_revision||'').slice(0,7))+'</td>'
                   +'<td>'+(r.tokens? r.tokens.total:'-')+'</td></tr>').join('')+'</table>'
                 :'<div class="empty">暂无</div>')+'</div>'};
function render(){
  $('main').innerHTML = VIEWS[view]();
  $('homeLabel').textContent = S.home;
  $('storeBadge').innerHTML = badge(S.store);
  if(view==='bootstrap' && !$('envText').value) fillEnvelope();
}

refresh().catch(e=>{ $('main').innerHTML = '<div class="card"><h2>加载失败</h2>'+pre(e.message)+'</div>'; });
</script>
</body>
</html>''';


if __name__ == '__main__':
    main()
