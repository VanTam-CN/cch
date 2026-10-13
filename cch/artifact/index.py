"""SQLite + FTS5 asset index (Artifact Library storage).

Stored under <cch-home>/artifact/ so it stays fully separate from the
Cognitive Base, its store.git, and any approval workflow. Program-side
ranking is deterministic: fixed feature weights, ties broken by asset_id.
"""
from __future__ import annotations
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ASSET_ID_RE = re.compile(r'^AST-\d{8}-\d{4}$')

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  source_file TEXT NOT NULL,
  page_or_slide INTEGER,
  embedded_ref TEXT,
  mime TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  document_sha256 TEXT NOT NULL,
  document_title TEXT NOT NULL,
  modified_at TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
  topics TEXT NOT NULL DEFAULT '[]',
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  extracted_text TEXT NOT NULL DEFAULT '[]',
  entities TEXT NOT NULL DEFAULT '[]',
  visual_type TEXT NOT NULL DEFAULT '',
  asset_status TEXT NOT NULL DEFAULT 'unknown',
  observed_at TEXT NOT NULL DEFAULT '',
  superseded_by TEXT,
  extraction_method TEXT NOT NULL DEFAULT '',
  job_id TEXT,
  confidence TEXT NOT NULL DEFAULT '',
  concepts TEXT NOT NULL DEFAULT '[]',
  enrichment_method TEXT NOT NULL DEFAULT '',
  enrichment_job_id TEXT NOT NULL DEFAULT '',
  aliases TEXT NOT NULL DEFAULT '[]',
  alias_method TEXT NOT NULL DEFAULT '',
  alias_job_id TEXT NOT NULL DEFAULT '',
  index_state TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_lineage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  old_asset_id TEXT NOT NULL,
  new_asset_id TEXT NOT NULL,
  source_file TEXT NOT NULL,
  page_or_slide INTEGER,
  embedded_ref TEXT,
  old_sha256 TEXT NOT NULL,
  new_sha256 TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  UNIQUE(old_asset_id, new_asset_id)
);
CREATE INDEX IF NOT EXISTS idx_assets_sha ON assets(sha256);
CREATE INDEX IF NOT EXISTS idx_assets_scope ON assets(domain, project);
CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
  asset_id UNINDEXED, title, summary, extracted_text, entities, topics,
  document_title, aliases,
  tokenize='trigram'
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect(root: Path) -> sqlite3.Connection:
    root = Path(root)
    artifact_dir = root/'artifact'
    artifact_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(artifact_dir/'asset-index.db')
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate_assets(conn)
    _migrate_fts(conn)
    return conn


def _migrate_assets(conn: sqlite3.Connection) -> None:
    """Add M2-B.1 L2 retrieval-metadata columns to pre-existing databases."""
    cols = {r[1] for r in conn.execute('PRAGMA table_info(assets)').fetchall()}
    if 'concepts' not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN concepts TEXT NOT NULL DEFAULT '[]'")
    if 'enrichment_method' not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN enrichment_method TEXT NOT NULL DEFAULT ''")
    if 'enrichment_job_id' not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN enrichment_job_id TEXT NOT NULL DEFAULT ''")
    if 'aliases' not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'")
    if 'alias_method' not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN alias_method TEXT NOT NULL DEFAULT ''")
    if 'alias_job_id' not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN alias_job_id TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _migrate_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS table if it predates document_title or aliases."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='assets_fts'").fetchone()
    if row is not None and 'document_title' in (row['sql'] or '') \
            and 'aliases' in (row['sql'] or ''):
        return
    conn.execute("DROP TABLE IF EXISTS assets_fts")
    conn.executescript(SCHEMA)
    for r in conn.execute("SELECT asset_id FROM assets").fetchall():
        _sync_fts(conn, r['asset_id'])
    conn.commit()


def _row_to_record(row: sqlite3.Row) -> dict:
    rec = dict(row)
    for field in ('topics', 'extracted_text', 'entities', 'concepts', 'aliases'):
        rec[field] = json.loads(rec[field])
    return rec


def mint_asset_id(conn: sqlite3.Connection) -> str:
    day = datetime.now(timezone.utc).strftime('%Y%m%d')
    prefix = f'AST-{day}-'
    row = conn.execute(
        "SELECT asset_id FROM assets WHERE asset_id LIKE ? ORDER BY asset_id DESC LIMIT 1",
        (prefix + '%',)).fetchone()
    seq = int(row['asset_id'][-4:]) + 1 if row else 1
    return f'{prefix}{seq:04d}'


def register_candidate(conn: sqlite3.Connection, cand, document_sha256: str,
                       modified_at: str, scope: dict) -> str:
    """Insert a scan candidate as a pending asset; returns its asset_id.

    Idempotent: an identical candidate (same document hash, page/slide,
    embedded ref, and content hash) is recognized and returned unchanged
    instead of minting a duplicate asset.
    """
    existing = conn.execute(
        "SELECT asset_id FROM assets WHERE document_sha256=?"
        " AND source_file=?"
        " AND COALESCE(page_or_slide, -1)=COALESCE(?, -1)"
        " AND COALESCE(embedded_ref, '')=COALESCE(?, '')"
        " AND sha256=?",
        (document_sha256, cand.source_path, cand.page,
         cand.embedded_ref, cand.sha256)).fetchone()
    if existing is not None:
        return existing['asset_id']
    # Content changed at a known logical position: record lineage
    # (old asset -> new asset) and flag the old asset as changed. Auto-
    # supersede stays forbidden; the human decision is unaffected.
    predecessors = conn.execute(
        "SELECT asset_id, sha256 FROM assets WHERE source_file=?"
        " AND COALESCE(page_or_slide, -1)=COALESCE(?, -1)"
        " AND COALESCE(embedded_ref, '')=COALESCE(?, '')"
        " AND sha256!=?",
        (cand.source_path, cand.page, cand.embedded_ref, cand.sha256)).fetchall()
    asset_id = mint_asset_id(conn)
    conn.execute(
        "INSERT INTO assets (asset_id, kind, source_file, page_or_slide, embedded_ref,"
        " mime, size_bytes, sha256, document_sha256, document_title, modified_at,"
        " domain, project, topics, index_state, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
        (asset_id, cand.kind, cand.source_path, cand.page, cand.embedded_ref,
         cand.mime, cand.size_bytes, cand.sha256, document_sha256,
         Path(cand.source_path).stem, modified_at,
         scope.get('domain', ''), scope.get('project', ''),
         json.dumps(scope.get('topics', []), ensure_ascii=False), _now()))
    _sync_fts(conn, asset_id)
    for pred in predecessors:
        conn.execute(
            "INSERT OR IGNORE INTO asset_lineage (old_asset_id, new_asset_id,"
            " source_file, page_or_slide, embedded_ref, old_sha256, new_sha256,"
            " observed_at) VALUES (?,?,?,?,?,?,?,?)",
            (pred['asset_id'], asset_id, cand.source_path, cand.page,
             cand.embedded_ref, pred['sha256'], cand.sha256, _now()))
        # Flag content drift on the old asset. A human 'superseded'
        # decision is never overwritten by the scanner.
        conn.execute(
            "UPDATE assets SET asset_status='changed' WHERE asset_id=?"
            " AND (asset_status IS NULL OR asset_status NOT IN ('superseded','changed'))",
            (pred['asset_id'],))
        _sync_fts(conn, pred['asset_id'])
    conn.commit()
    return asset_id


def apply_semantics(conn: sqlite3.Connection, asset_id: str, semantic: dict,
                    job_id: str, extraction_method: str, commit: bool = True) -> None:
    """Fill VLM/parse-derived fields and move pending -> indexed."""
    row = conn.execute("SELECT index_state FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None:
        raise KeyError(asset_id)
    conn.execute(
        "UPDATE assets SET title=?, summary=?, extracted_text=?, entities=?, visual_type=?,"
        " observed_at=?, extraction_method=?, job_id=?, confidence=?,"
        " index_state='indexed' WHERE asset_id=?",
        (semantic.get('title', ''), semantic.get('summary', ''),
         json.dumps(semantic.get('extracted_text', []), ensure_ascii=False),
         json.dumps(semantic.get('entities', []), ensure_ascii=False),
         semantic.get('visual_type', ''), semantic.get('observed_at', ''), extraction_method, job_id,
        semantic.get('confidence', ''), asset_id))
    _sync_fts(conn, asset_id)
    if commit:
        conn.commit()


CONCEPT_MAX = 12
CONCEPT_LEN_MIN = 2
CONCEPT_LEN_MAX = 16
CONCEPT_LOWINFO = {'本页', '项目', '研究', '技术', '系统', '内容', '相关', '材料', '文档', '报告', '图表', '页面'}
# L3 alias bounds (/3 contract): colloquial query bridges, slightly
# longer phrases allowed, tighter count ceiling (stuffing guard).
ALIAS_MIN = 3
ALIAS_MAX = 8
ALIAS_LEN_MIN = 4
ALIAS_LEN_MAX = 24
ALIAS_LOWINFO = {'这个素材', '这份材料', '相关内容', '相关资料', '这个问题', '这个页面'}


def validate_concepts(concepts) -> list[str]:
    """Validate /2 per-asset concepts (L2): grounded, narrow, bounded.

    Rules (docs/M2-B1-Semantic-Retrieval-Plan.md, user-constrained):
    3-12 items, each 2-16 chars, noun-phrase style, deduplicated,
    low-information generic words dropped. No aliases here - query
    bridging belongs to L3.
    """
    if not isinstance(concepts, list):
        raise SystemExit('INVALID_CONCEPTS: concepts must be a list of strings')
    cleaned: list[str] = []
    for c in concepts:
        if not isinstance(c, str):
            raise SystemExit('INVALID_CONCEPTS: concepts must be a list of strings')
        c = c.strip()
        if not c:
            continue
        if not (CONCEPT_LEN_MIN <= len(c) <= CONCEPT_LEN_MAX):
            raise SystemExit(f'INVALID_CONCEPTS: concept length 2-16 required, got {c!r}')
        if c in CONCEPT_LOWINFO:
            continue
        if c not in cleaned:
            cleaned.append(c)
    if not (3 <= len(cleaned) <= CONCEPT_MAX):
        raise SystemExit(f'INVALID_CONCEPTS: 3-12 concepts required, got {len(cleaned)}')
    return cleaned


def apply_concept_enrichment(conn: sqlite3.Connection, asset_id: str,
                             concepts: list[str], method: str, job_id: str) -> None:
    """In-place retrieval-metadata enrichment (L2). Governance-neutral:
    never touches asset_status, index_state, lineage, or Cognitive Base.
    """
    cleaned = validate_concepts(concepts)
    row = conn.execute('SELECT asset_id FROM assets WHERE asset_id=?', (asset_id,)).fetchone()
    if row is None:
        raise KeyError(asset_id)
    conn.execute(
        'UPDATE assets SET concepts=?, enrichment_method=?, enrichment_job_id=?'
        ' WHERE asset_id=?',
        (json.dumps(cleaned, ensure_ascii=False), method, job_id, asset_id))
    _sync_fts(conn, asset_id)


def validate_aliases(aliases) -> list[str]:
    """Validate /3 per-asset aliases (L3): colloquial query bridges,
    bounded, dedup, low-info dropped. Governance-neutral retrieval
    metadata only.
    """
    if not isinstance(aliases, list):
        raise SystemExit('INVALID_ALIASES: aliases must be a list of strings')
    cleaned: list[str] = []
    for a in aliases:
        if not isinstance(a, str):
            raise SystemExit('INVALID_ALIASES: aliases must be a list of strings')
        a = a.strip()
        if not a:
            continue
        if not (ALIAS_LEN_MIN <= len(a) <= ALIAS_LEN_MAX):
            raise SystemExit(f'INVALID_ALIASES: alias length {ALIAS_LEN_MIN}-{ALIAS_LEN_MAX} required, got {a!r}')
        if a in ALIAS_LOWINFO:
            continue
        if a not in cleaned:
            cleaned.append(a)
    if not (ALIAS_MIN <= len(cleaned) <= ALIAS_MAX):
        raise SystemExit(f'INVALID_ALIASES: {ALIAS_MIN}-{ALIAS_MAX} aliases required, got {len(cleaned)}')
    return cleaned


def apply_alias_enrichment(conn: sqlite3.Connection, asset_id: str,
                           aliases: list[str], method: str, job_id: str) -> None:
    """In-place retrieval-metadata enrichment (L3). Governance-neutral:
    never touches asset_status, index_state, lineage, Cognitive Base,
    or the /2 concepts columns.
    """
    cleaned = validate_aliases(aliases)
    row = conn.execute('SELECT asset_id FROM assets WHERE asset_id=?', (asset_id,)).fetchone()
    if row is None:
        raise KeyError(asset_id)
    conn.execute(
        'UPDATE assets SET aliases=?, alias_method=?, alias_job_id=?'
        ' WHERE asset_id=?',
        (json.dumps(cleaned, ensure_ascii=False), method, job_id, asset_id))
    _sync_fts(conn, asset_id)


def _sync_fts(conn: sqlite3.Connection, asset_id: str) -> None:
    row = conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    conn.execute("DELETE FROM assets_fts WHERE asset_id=?", (asset_id,))
    conn.execute(
        "INSERT INTO assets_fts (asset_id, title, summary, extracted_text, entities,"
        " topics, document_title, aliases) VALUES (?,?,?,?,?,?,?,?)",
        (asset_id, row['title'], row['summary'], ' '.join(json.loads(row['extracted_text'])),
         ' '.join(json.loads(row['entities'])), ' '.join(json.loads(row['topics'])),
         row['document_title'], ' '.join(json.loads(row['aliases']))))


def get_asset(root: Path, asset_id: str) -> dict:
    if not ASSET_ID_RE.match(asset_id):
        raise SystemExit(f'VALIDATION_ERROR: bad asset id {asset_id!r}')
    row = connect(root).execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None:
        raise SystemExit(f'NOT_FOUND: asset {asset_id}')
    return _row_to_record(row)


def mark_superseded(root: Path, asset_id: str, superseded_by: str | None) -> dict:
    conn = connect(root)
    if superseded_by is not None:
        if conn.execute("SELECT 1 FROM assets WHERE asset_id=?", (superseded_by,)).fetchone() is None:
            raise SystemExit(f'NOT_FOUND: superseding asset {superseded_by}')
    conn.execute("UPDATE assets SET asset_status='superseded', superseded_by=? WHERE asset_id=?",
                 (superseded_by, asset_id))
    conn.commit()
    return get_asset(root, asset_id)


def stats(root: Path) -> dict:
    conn = connect(root)
    total, = conn.execute("SELECT COUNT(*) FROM assets").fetchone()
    pending, = conn.execute("SELECT COUNT(*) FROM assets WHERE index_state='pending'").fetchone()
    superseded, = conn.execute(
        "SELECT COUNT(*) FROM assets WHERE asset_status='superseded'").fetchone()
    return {'assets': total, 'pending': pending, 'superseded': superseded,
            'index_path': str(Path(root)/'artifact'/'asset-index.db')}


def list_assets(root: Path, state: str = 'all', limit: int = 500) -> list[dict]:
    """List assets for review and gold labeling (asset_id + sha256 + locator)."""
    if state not in ('all', 'pending', 'indexed'):
        raise SystemExit('VALIDATION_ERROR: state must be all|pending|indexed')
    conn = connect(root)
    if state == 'all':
        rows = conn.execute(
            "SELECT * FROM assets ORDER BY asset_id LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM assets WHERE index_state=? ORDER BY asset_id LIMIT ?",
            (state, limit)).fetchall()
    return [_row_to_record(r) for r in rows]


# Deterministic ranking weights (v0.3.5 unified formula, applies to both
# the FTS path and the topic-recall channel):
#   scope boosts dominate (2 x 1000), then lexical strength (-bm25: SQLite
#   returns negative bm25 where more negative = stronger match; v0.3.4
#   ranked by -abs(bm25), which inverted strength), then topic surface
#   evidence (title mention +30, summary mention +10, tag intersection +2).
WEIGHT_SCOPE_EXACT = 1000.0
WEIGHT_TITLE_TOPIC = 30.0
WEIGHT_SUMMARY_TOPIC = 10.0
WEIGHT_TAG_TOPIC = 2.0
WEIGHT_CONCEPT_MATCH = 2.0

# v0.4.0 (M2-B.1 QW) corpus-derived query-informativeness weighting.
# Mechanism defect fixed (v0.3.9 gate post-mortem, read-only): long
# colloquial queries produced many generic bigrams that scored the same
# as rare discriminative anchors, so wordy pages outranked the target
# page. Fix is mechanism-level, task-agnostic: each query bigram gets a
# weight from corpus document frequency (df) over the frozen index --
# df=1 keeps the full unit weight, df>=2 decays as unit/df, clipped to
# QUERY_WEIGHT_FLOOR. Bonus is normalized by the query's TOTAL
# information mass, so padding a query with low-information phrases
# cannot inflate a score. No heldout targets, no failure-task
# vocabularies, no online LLM; deterministic given the index.
QUERY_WEIGHT_FLOOR = 0.25

# v0.3.5 lexical equivalence classes (failure-mode 3 fix): colloquial
# query wording and document titles often name the same artifact type
# differently ("整体设计图" vs "系统架构图"). Expansion happens BEFORE
# windowing and feeds the same bm25 ranking, so it stays deterministic
# and query-agnostic; reported per search for transparency.
TERM_SYNONYMS = {
    '设计图': ['架构图', '结构图'],
    '架构图': ['设计图', '结构图'],
    '结构图': ['设计图', '架构图'],
}

# v0.3.6 (M2-B.1 L1) deterministic query decomposition: unsegmented
# Chinese sentences previously produced only 3-6 char sliding windows,
# so key 2-char concepts never became recall signals even when asset
# summaries literally contained them ("工作单位与任务分工", "低空区域
# 网络架构"). Queries are now split on punctuation, stopword characters
# are stripped, and the remaining informative 2-char concepts are
# recalled through a deterministic LIKE channel (the trigram FTS index
# cannot match 2-char phrases). No online LLM, no per-task rules.
QUERY_STOPWORDS = {
    '这个', '那个', '哪些', '什么', '怎么', '怎样', '如何', '为什么',
    '一下', '我们', '你们', '他们', '之前', '还有', '以及', '但是',
    '就是', '都有', '有些', '找一下', '看一下',
}
_QUERY_STOP_CHARS = set('的了吗呢吧啊呀在是有和与或把对被不没很就要用看说讲你我这那哪个些都还找帮给从到会能')
_QUERY_SPLIT_CHARS = set('，。！？、；：,.!?;:（）()【】《》…— ' + chr(9) + chr(10) + chr(13))
# v0.4.0 (QW) numeral-measure class rule (task-agnostic Chinese query
# decomposition): bigrams of the form <CJK numeral><measure word>
# ("一页", "一张", "一份") express COUNTING, not content. They are
# frequently page-reference noise ("那一页") that corpus DF can
# accidentally crown as a rare high-weight anchor when the corpus
# itself rarely mentions pages. RC fix (77244cc review): the class is
# deliberately narrow -- pure navigation measure words ONLY. "步/项/
# 类/套/条..." regularly carry real content ("三步路线", "三项关键
# 技术", "两类场景"), so dropping them would delete legitimate anchors
# from the query. Dropped at decomposition time by linguistic class,
# never by target vocabulary.
_QUERY_MEASURE_CHARS = set('页张份')
_QUERY_NUMERAL_CHARS = set('一二三四五六七八九十百千两')


def _query_concepts(query: str) -> dict:
    """Deterministic L1 query decomposition (M2-B.1).

    Returns {'blocks': punctuation-split content blocks (>=2 chars),
    'bigrams': informative 2-char concepts in first-seen order}.
    """
    blocks: list[str] = []
    cur: list[str] = []
    for ch in query:
        if ch in _QUERY_SPLIT_CHARS:
            if cur:
                blocks.append(''.join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        blocks.append(''.join(cur))
    blocks = [b for b in blocks if len(b) >= 2]
    bigrams: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        cleaned = ''.join(ch for ch in block if ch not in _QUERY_STOP_CHARS)
        for i in range(len(cleaned) - 1):
            bg = cleaned[i:i + 2]
            if (bg[0] in _QUERY_NUMERAL_CHARS
                    and bg[1] in _QUERY_MEASURE_CHARS):
                continue
            if bg and bg not in seen and bg not in QUERY_STOPWORDS:
                seen.add(bg)
                bigrams.append(bg)
    return {'blocks': blocks, 'bigrams': bigrams}


def _corpus_weights(conn: sqlite3.Connection, bigrams: list[str]) -> dict:
    """v0.4.0 (QW) deterministic corpus DF -> weight per query bigram.

    df = number of indexed assets whose searchable surface (title,
    summary, document_title, entities, topics) contains the bigram.
    Rarer anchors are more informative: df<=1 keeps the full unit
    weight (WEIGHT_CONCEPT_MATCH); df>=2 decays as unit/df; everything
    is clipped to [QUERY_WEIGHT_FLOOR, WEIGHT_CONCEPT_MATCH]. Derived
    solely from corpus statistics of the frozen index.
    """
    if not bigrams:
        return {}
    n_indexed = conn.execute(
        "SELECT COUNT(*) AS n FROM assets WHERE index_state='indexed'"
    ).fetchone()['n']
    weights: dict[str, float] = {}
    for bg in bigrams:
        if n_indexed <= 1:
            weights[bg] = WEIGHT_CONCEPT_MATCH
            continue
        like = '%' + bg + '%'
        df = conn.execute(
            "SELECT COUNT(*) AS n FROM assets WHERE index_state='indexed' AND ("
            "title LIKE ? OR summary LIKE ? OR document_title LIKE ?"
            " OR entities LIKE ? OR topics LIKE ?)",
            (like, like, like, like, like)).fetchone()['n']
        if df == 0:
            # Never attested in the corpus: no informativeness evidence,
            # so it contributes only floor mass to the query total (and
            # cannot dilute anchors via an inflated unit weight).
            weights[bg] = QUERY_WEIGHT_FLOOR
        else:
            weights[bg] = round(max(QUERY_WEIGHT_FLOOR,
                                    min(WEIGHT_CONCEPT_MATCH,
                                        WEIGHT_CONCEPT_MATCH / df)), 4)
    return weights


def search(root: Path, query: str, domain: str | None = None,
           project: str | None = None, topics: list[str] | None = None,
           limit: int = 10, strict_scope: bool = True,
           include_superseded: bool = False) -> dict:
    """Task-driven retrieval with scope-aware deterministic scoring.

    strict_scope (default): assets whose domain/project explicitly
    mismatches the query scope are excluded, so cross-project assets can
    never slip into Top-K. Pass strict_scope=False (expanded scope) to
    keep the older behavior of merely boosting matching assets.

    include_superseded (default False): superseded assets are excluded
    from results entirely unless explicitly requested (history/evolution
    comparison use cases).
    """
    conn = connect(root)
    query = query.strip()
    if not query:
        raise SystemExit('VALIDATION_ERROR: empty query')
    query_concepts = _query_concepts(query)
    terms = query_concepts['blocks'] or [query]
    expansions = []
    expanded_terms = []
    for t in terms:
        expanded_terms.append(t)
        for key, syns in TERM_SYNONYMS.items():
            # Chinese queries are unsegmented, so a synonym key matches
            # when it appears anywhere inside the query term.
            if key in t:
                for syn in syns:
                    if syn not in expanded_terms:
                        expanded_terms.append(syn)
                        expansions.append(f'{key}->{syn}')
    terms = expanded_terms
    def _windows(term: str) -> list[str]:
        # Trigram phrase matching needs the query string to appear
        # contiguously in the content. Long compound terms miss when the
        # content inserts one connecting character, so OR together every
        # 3-6 char sliding window plus the full term: any shared sub-span
        # recalls the asset, and bm25 favors more/better matches.
        if len(term) <= 3:
            return [term]
        wins, seen = [], set()
        for size in range(3, min(6, len(term)) + 1):
            for start in range(0, len(term) - size + 1):
                w = term[start:start + size]
                if w not in seen:
                    seen.add(w)
                    wins.append(w)
        wins.append(term)
        return wins
    quoted = []
    for t in terms:
        for w in _windows(t):
            quoted.append('"' + w.replace('"', '""') + '"')
    match_expr = ' OR '.join(quoted)
    try:
        hits = conn.execute(
            "SELECT asset_id, bm25(assets_fts) AS rank FROM assets_fts"
            " WHERE assets_fts MATCH ? ORDER BY rank, asset_id LIMIT 200",
            (match_expr,)).fetchall()
    except sqlite3.OperationalError:
        hits = []
    if not hits:
        seen_ids, fallback = [], []
        for term in terms:
            esc = term.replace('\\', '\\\\').replace('%', '\%').replace('_', '\_')
            like = f'%{esc}%'
            rows = conn.execute(
                "SELECT asset_id FROM assets WHERE title LIKE ? OR summary LIKE ?"
                " ESCAPE '\\' OR document_title LIKE ? ESCAPE '\\'"
                " OR topics LIKE ? ESCAPE '\\'"
                " ORDER BY asset_id LIMIT 200", (like, like, like, like)).fetchall()
            for r in rows:
                if r['asset_id'] not in seen_ids:
                    seen_ids.append(r['asset_id'])
                    fallback.append({'asset_id': r['asset_id'], 'rank': 0.0})
        hits = fallback
    topics = topics or []
    query_topics = {t.strip() for t in topics if t and t.strip()}

    def _topic_surface_bonus(row) -> tuple[float, list[str]]:
        matched = set(json.loads(row['topics'])).intersection(query_topics)
        if not matched:
            return 0.0, []
        bonus = WEIGHT_TAG_TOPIC * len(matched)
        if any(t in row['title'] for t in matched):
            bonus += WEIGHT_TITLE_TOPIC
        if any(t in row['summary'] for t in matched):
            bonus += WEIGHT_SUMMARY_TOPIC
        return bonus, sorted(matched)

    bigrams = query_concepts['bigrams']

    # v0.4.0 (QW): corpus-derived per-bigram informativeness weights +
    # total query information mass for the normalized evidence bonus.
    weights = _corpus_weights(conn, bigrams)
    total_info = sum(weights.values())

    def _concept_evidence(row) -> dict:
        # L2/L3 semantic channel: a query bigram hits a concept (L2)
        # or alias (L3) when it appears inside it ("低空" in "低空网络
        # 架构"). L2.1 returns the mapping query-evidence -> matched
        # metadata so scoring counts DISTINCT QUERY EVIDENCE instead of
        # matched entries: restating one anchor as several concepts or
        # aliases must not multiply the bonus (semantic metadata
        # stuffing guard, now alias-aware for L3). The full mapping
        # stays available for the audit trail in `why`. v0.4.0 (QW):
        # only metadata-derived evidence lives here; a bigram matching
        # bare title/summary text is a SURFACE match and is itemized by
        # the surface-bigram audit below -- same anchor, one bonus.
        evidence = {}
        semantic_items = json.loads(row['concepts']) + json.loads(row['aliases'] or '[]')
        for c in semantic_items:
            for bg in bigrams:
                if bg in c:
                    bucket = evidence.setdefault(bg, [])
                    if c not in bucket:
                        bucket.append(c)
        return evidence

    scored = []
    for hit in hits:
        row = conn.execute("SELECT * FROM assets WHERE asset_id=?",
                           (hit['asset_id'],)).fetchone()
        if not include_superseded and row['asset_status'] == 'superseded':
            continue
        if strict_scope and domain and row['domain'] and row['domain'] != domain:
            continue
        if strict_scope and project and row['project'] and row['project'] != project:
            continue
        # v0.4.0 (QW): pool ENTRY carries no credit on any channel.
        # bm25 orders the 200-row FTS recall boundary only; absolute
        # bm25 magnitudes (O(1-10), match-density driven) previously
        # dwarfed the query-mass-normalized evidence bonus (O(0-1)) and
        # channel entry credits differed (FTS ~1.0 vs concept 0), so
        # wordy pages could outrank rare-anchor pages purely via pool
        # provenance. Cross-pool ranking is now scope boosts + topic
        # bonus + corpus-DF-weighted evidence bonus, identical for
        # every channel; ties break deterministically by asset_id.
        score = 0.0
        why = []
        if domain and row['domain'] == domain:
            score += WEIGHT_SCOPE_EXACT
            why.append(f'domain={domain}')
        if project and row['project'] == project:
            score += WEIGHT_SCOPE_EXACT
            why.append(f'project={project}')
        row_topics = set(json.loads(row['topics']))
        topic_bonus, matched_topics = _topic_surface_bonus(row)
        score += topic_bonus
        if matched_topics:
            why.append('topics=' + ','.join(matched_topics))
        if row['asset_status'] == 'superseded':
            score -= 2 * WEIGHT_SCOPE_EXACT
            why.append('SUPERSEDED')
        elif row['asset_status'] == 'unknown':
            why.append('status-unknown')
        if row['index_state'] != 'indexed':
            why.append('PENDING-SEMANTICS')
        scored.append({'score': round(score, 4), 'why': why,
                       'asset': _row_to_record(row)})
    # v0.3.5 topic-recall channel: assets whose indexed topics contain a
    # query topic are candidates even when the free text shares no lexical
    # window with the query (paraphrase queries). Same score formula as
    # the FTS path (lexical absent => 0), so strong lexical matches outrank
    # title-only topic matches, which outrank weak/no lexical matches.
    seen_ids = {item['asset']['asset_id'] for item in scored}
    if query_topics:
        channel_rows = conn.execute(
            "SELECT * FROM assets WHERE index_state='indexed'").fetchall()
        for row in channel_rows:
            aid = row['asset_id']
            if aid in seen_ids:
                continue
            if not include_superseded and row['asset_status'] == 'superseded':
                continue
            if strict_scope and domain and row['domain'] and row['domain'] != domain:
                continue
            if strict_scope and project and row['project'] and row['project'] != project:
                continue
            row_topics = set(json.loads(row['topics']))
            matched = row_topics.intersection(query_topics)
            if not matched:
                continue
            c_score = 2 * WEIGHT_SCOPE_EXACT + _topic_surface_bonus(row)[0]
            why = ['topic-channel', 'topics=' + ','.join(sorted(matched))]
            if domain and row['domain'] == domain:
                why.append(f'domain={domain}')
            if project and row['project'] == project:
                why.append(f'project={project}')
            if row['asset_status'] == 'unknown':
                why.append('status-unknown')
            scored.append({'score': round(c_score, 4), 'why': why,
                           'asset': _row_to_record(row)})
            seen_ids.add(aid)
    # v0.3.6 (M2-B.1 L1) 2-char concept recall channel: the trigram FTS
    # index cannot match 2-char phrases, so informative 2-char query
    # concepts are recalled through deterministic LIKE matching against
    # title/summary/document_title/topics/entities. Ranking stays the
    # unified formula with 0 lexical credit (substring evidence is not
    # phrase evidence); each distinct matched concept adds the same +2
    # used for tag evidence, so assets covering more query concepts
    # outrank generic-overlap pages. Query-agnostic, no per-task rules.
    if bigrams:
        for row in conn.execute(
                "SELECT * FROM assets WHERE index_state='indexed'").fetchall():
            aid = row['asset_id']
            if aid in seen_ids:
                continue
            if not include_superseded and row['asset_status'] == 'superseded':
                continue
            if strict_scope and domain and row['domain'] and row['domain'] != domain:
                continue
            if strict_scope and project and row['project'] and row['project'] != project:
                continue
            haystack = ' '.join([
                row['title'], row['summary'], row['document_title'],
                ' '.join(json.loads(row['entities'])),
                ' '.join(json.loads(row['topics']))])
            matched = [bg for bg in bigrams if bg in haystack]
            if not matched:
                continue
            # v0.4.0 (QW): surface bigram hits are candidate-pool entry
            # with ZERO entry credit, same as every other channel. The
            # evidence bonus is applied ONCE per anchor in the unified
            # post-pass (corpus-DF weighted, query-mass normalized);
            # per-hit equal weighting was the dilution defect removed
            # in this version.
            b_score = 0.0
            topic_bonus, matched_topics = _topic_surface_bonus(row)
            b_score += topic_bonus
            why = ['surface-bigram=' + ','.join(matched[:6])]
            if domain and row['domain'] == domain:
                b_score += WEIGHT_SCOPE_EXACT
                why.append(f'domain={domain}')
            if project and row['project'] == project:
                b_score += WEIGHT_SCOPE_EXACT
                why.append(f'project={project}')
            if matched_topics:
                why.append('topics=' + ','.join(matched_topics))
            if row['asset_status'] == 'unknown':
                why.append('status-unknown')
            scored.append({'score': round(b_score, 4), 'why': why,
                           'asset': _row_to_record(row)})
            seen_ids.add(aid)
    # v0.3.7 (M2-B.1 L2) per-asset concept channel: concepts are grounded
    # VLM retrieval metadata (/2), separate from coarse inherited topics.
    # v0.3.9 (M2-B.1 L3): /3 aliases ("how users might ask") share this
    # channel — alias hits enter the same DISTINCT QUERY EVIDENCE key
    # space via _concept_evidence, so richer alias metadata cannot
    # multiply-score a single anchor (L2.1 stuffing guard inherits).
    # (L2.1 note: the per-asset bonus is applied in the post-pass below
    # as DISTINCT QUERY EVIDENCE units, not per matched entry.)
    # Matching is modest on purpose: concept hits guarantee candidate-
    # pool entry and add a small bonus; they do not dominate ranking
    # (docs/M2-B1-Semantic-Retrieval-Plan.md, weight guidance).
    if bigrams:
        for row in conn.execute(
                "SELECT * FROM assets WHERE index_state='indexed'").fetchall():
            aid = row['asset_id']
            if aid in seen_ids:
                continue
            if not include_superseded and row['asset_status'] == 'superseded':
                continue
            if strict_scope and domain and row['domain'] and row['domain'] != domain:
                continue
            if strict_scope and project and row['project'] and row['project'] != project:
                continue
            evidence = _concept_evidence(row)
            if not evidence:
                continue
            # Pool entry only; the concept bonus is applied uniformly in
            # the post-pass below, so evidence is never double-counted.
            c_score = 0.0
            topic_bonus, matched_topics = _topic_surface_bonus(row)
            c_score += topic_bonus
            why = ['concept-channel']
            if domain and row['domain'] == domain:
                c_score += WEIGHT_SCOPE_EXACT
                why.append(f'domain={domain}')
            if project and row['project'] == project:
                c_score += WEIGHT_SCOPE_EXACT
                why.append(f'project={project}')
            if matched_topics:
                why.append('topics=' + ','.join(matched_topics))
            if row['asset_status'] == 'unknown':
                why.append('status-unknown')
            if not row['enrichment_method']:
                why.append('NO-CONCEPTS')
            scored.append({'score': round(c_score, 4), 'why': why,
                           'asset': _row_to_record(row)})
            seen_ids.add(aid)
    # v0.4.0 (QW) unified evidence post-pass (evolves the v0.3.8 L2.1
    # per-evidence bonus): each DISTINCT query anchor scores once, the
    # bonus is its corpus-DF weight (rare anchor up to +2.0, generic
    # fragments decay to the +0.25 floor), the sum is normalized by the
    # query's TOTAL information mass, and surface + metadata hits of
    # the same anchor merge into one unit (no channel double-count).
# A longer query carries more total mass, so padding with
# low-information phrases cannot inflate a match. Precision note:
# within ONE query the normalization denominator is a common constant,
# so generic-vs-anchor separation comes from the inverse-DF
# down-weighting itself; normalization stabilizes the bonus scale
# across queries of different length and keeps it commensurate with
# topic bonus. Deterministic given the index; no heldout targets, no
# failure-task vocabulary.
    if bigrams:
        for item in scored:
            row = conn.execute('SELECT * FROM assets WHERE asset_id=?',
                               (item['asset']['asset_id'],)).fetchone()
            haystack = ' '.join([
                row['title'], row['summary'], row['document_title'],
                ' '.join(json.loads(row['entities'])),
                ' '.join(json.loads(row['topics']))])
            surface_hits = [bg for bg in bigrams if bg in haystack]
            meta_evidence = _concept_evidence(row)
            combined = dict.fromkeys(bigrams, [])
            for bg in surface_hits:
                combined[bg] = ['<surface>']
            for bg, cs in meta_evidence.items():
                combined[bg] = cs
            evidence = {bg: cs for bg, cs in combined.items() if cs}
            if evidence and total_info > 0:
                bonus = sum(weights.get(bg, QUERY_WEIGHT_FLOOR)
                            for bg in evidence) / total_info
                item['score'] = round(item['score'] + bonus, 4)
                wpart = ';'.join(bg + '=' + str(weights.get(bg, QUERY_WEIGHT_FLOOR))
                                 for bg in sorted(evidence))
                item['why'].append('qw-evidence=' + wpart)
                mapping = ';'.join(bg + ':' + '|'.join(cs[:2])
                                   for bg, cs in sorted(evidence.items())[:3])
                item['why'].append('concept-evidence=' + mapping)
    scored.sort(key=lambda item: (-item['score'], item['asset']['asset_id']))
    return {'query': query, 'total_candidates': len(scored),
            'synonym_expansions': expansions,
            'note': '检索不到只说明本次查询未命中，不代表素材不存在；可扩大范围或回退原始资料。',
            'results': scored[:max(1, limit)]}
