import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
from cch.core import (Base,CCHError,TokenCounter,compile_context,config_load,governance,instant,retrieve)

NOW=instant('2026-09-07T00:00:00Z')
CONFIG=config_load()

def obj(oid='FACT-001', **updates):
    o={'id':oid,'type':'FACT','scope':{'domain':'test','project':'p','topic':[]},'status':'ACTIVE',
       'confidence':'CONFIRMED','authority':'USER_CONFIRMED','priority':'REFERENCE','content':'连接池 配置 pool setting',
       'rationale':None,'relations':{r:[] for r in ['depends_on','supersedes','conflicts_with','derived_from']},
       'provenance':{'evidence_refs':[]},'lifecycle':{'valid_from':'2026-09-01','valid_until':None,'ttl_hours':None},
       'retrieval':{'keywords':['连接池'],'aliases':[]},'tags':['secret-diagnostic']}
    o.update(updates)
    return o

class CompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.counter=TokenCounter(CONFIG['tokenizer'])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'cognitive-base').mkdir()
        (self.root/'evidence').mkdir()
    def tearDown(self):
        self.temp.cleanup()
    def write(self,objects):
        for n,o in enumerate(objects):
            (self.root/f'cognitive-base/{n}.yaml').write_text(yaml.safe_dump(o,allow_unicode=True),encoding='utf-8')
        return Base(self.root)
    def run_compile(self,base,query='连接池',budget=10000,**kw):
        return compile_context(base,query,'test','p',[],NOW,budget,CONFIG,counter=self.counter,**kw)
    def test_scope_isolation_and_redacted_links(self):
        a=obj(); b=obj('FACT-002',scope={'domain':'test','project':'other','topic':[]},content='private-secret')
        a['relations']['depends_on']=['FACT-002']
        text,audit=self.run_compile(self.write([a,b]))
        self.assertNotIn('private-secret',text); self.assertNotIn('FACT-002',text)
        self.assertEqual(audit['governance']['FACT-001']['effective_status'],'NEEDS_REVIEW')
    def test_topic_requires_explicit_match(self):
        a=obj(scope={'domain':'test','project':'p','topic':['private']})
        text,audit=self.run_compile(self.write([a]))
        self.assertEqual(audit['selected'],[])
    def test_global_rule_applies(self):
        a=obj('RULE-001',type='RULE',priority='MUST',scope={'domain':'*','project':'*','topic':[]})
        _,audit=self.run_compile(self.write([a]),query='unrelated')
        self.assertEqual(audit['selected'],['RULE-001'])
    def test_expiration_boundary(self):
        a=obj(lifecycle={'valid_from':'2026-09-06','ttl_hours':24,'valid_until':None})
        b=self.write([a]); s=governance(b,'test','p',[],NOW)
        self.assertFalse(s['FACT-001']['eligible'])
    def test_future_not_selected(self):
        a=obj(lifecycle={'valid_from':'2026-09-08','ttl_hours':None,'valid_until':None})
        _,a=self.run_compile(self.write([a])); self.assertEqual(a['selected'],[])
    def test_transitive_invalid_dependency(self):
        a=obj(); b=obj('FACT-002'); c=obj('DEC-003',type='DECISION',status='SUPERSEDED')
        a['relations']['depends_on']=['FACT-002']; b['relations']['depends_on']=['DEC-003']
        base=self.write([a,b,c]); old=base.source_hash
        _,audit=self.run_compile(base)
        self.assertEqual(audit['governance']['FACT-001']['effective_status'],'NEEDS_REVIEW')
        self.assertEqual(base.objects['FACT-001']['status'],'ACTIVE'); self.assertEqual(old,Base(self.root).source_hash)
    def test_uncertainty_survives_dependency(self):
        a=obj(); b=obj('FACT-002',confidence='TENTATIVE'); a['relations']['depends_on']=['FACT-002']
        text,audit=self.run_compile(self.write([a,b])); self.assertIn('TENTATIVE',text)
        self.assertIn('DEPENDENCY_UNCERTAIN',audit['governance']['FACT-001']['qualifications'])
    def test_confirmed_conflict_needs_review(self):
        a=obj(); b=obj('FACT-002'); a['relations']['conflicts_with']=['FACT-002']
        _,audit=self.run_compile(self.write([a,b]))
        for oid in ['FACT-001','FACT-002']:
            self.assertEqual(audit['governance'][oid]['effective_status'],'NEEDS_REVIEW')
    def test_two_active_supersession_is_not_silently_resolved(self):
        a=obj(); b=obj('FACT-002'); b['relations']['supersedes']=['FACT-001']
        states=governance(self.write([a,b]),'test','p',[],NOW)
        self.assertEqual(states['FACT-001']['effective_status'],'NEEDS_REVIEW')
    def test_invalid_objects_excluded(self):
        for status in ['SUPERSEDED','REJECTED','ARCHIVED']:
            with self.subTest(status=status):
                _,audit=self.run_compile(self.write([obj(status=status)]))
                self.assertEqual(audit['selected'],[])
    def test_duplicate_id_rejected(self):
        with self.assertRaises(CCHError): self.write([obj(),obj()])
    def test_duplicate_yaml_key_rejected(self):
        (self.root/'cognitive-base/bad.yaml').write_text('id: A\nid: B\n')
        with self.assertRaises(CCHError): Base(self.root)
    def test_missing_reference_rejected(self):
        a=obj(); a['relations']['depends_on']=['MISSING']
        with self.assertRaises(CCHError): self.write([a])
    def test_cycle_rejected(self):
        a=obj(); b=obj('FACT-002'); a['relations']['depends_on']=['FACT-002']; b['relations']['depends_on']=['FACT-001']
        with self.assertRaises(CCHError): self.write([a,b])
    def test_diamond_is_valid(self):
        a,b,c,d=[obj(f'FACT-00{i}') for i in range(1,5)]
        a['relations']['depends_on']=['FACT-002','FACT-003']
        b['relations']['depends_on']=['FACT-004']; c['relations']['depends_on']=['FACT-004']
        self.write([a,b,c,d])
    def test_invalid_ttl_rejected(self):
        for ttl in [-1,0,True,float('inf')]:
            with self.subTest(ttl=ttl),self.assertRaises(CCHError):
                self.write([obj(lifecycle={'valid_from':'2026-09-01','ttl_hours':ttl})])
    def test_evidence_path_escape_rejected(self):
        (self.root/'evidence/evidence-index.yaml').write_text('EV-001:\n  path: ../outside.md\n  summary: bad\n')
        with self.assertRaises(CCHError): self.write([obj()])
    def test_symlink_rejected(self):
        (self.root/'cognitive-base/link.yaml').symlink_to(self.root/'outside.yaml')
        with self.assertRaises(CCHError): self.write([obj()])
    def test_field_allowlist_and_actual_budget(self):
        text,audit=self.run_compile(self.write([obj()]))
        self.assertNotIn('secret-diagnostic',text); self.assertNotIn('retrieval',text)
        self.assertEqual(audit['tokens'],self.counter.count(text)); self.assertLessEqual(audit['tokens'],audit['budget'])
    def test_mandatory_overflow_fails(self):
        base=self.write([obj('RULE-001',type='RULE',priority='MUST')])
        with self.assertRaises(CCHError) as exc: self.run_compile(base,budget=1)
        self.assertEqual(exc.exception.code,'BUDGET_EXCEEDED')
    def test_atomic_dependency_bundle(self):
        a=obj(); b=obj('FACT-002',content='large '+('x '*3000)); a['relations']['depends_on']=['FACT-002']
        base=self.write([a,b]); text,audit=self.run_compile(base,query='FACT-001',budget=1000)
        self.assertNotIn('FACT-001',audit['selected']); self.assertLessEqual(audit['tokens'],1000)
    def test_full_evidence_is_exact_and_linked(self):
        source='> 可能采用此方案。\n不构成正式决定。\n'
        (self.root/'evidence/EV-001.md').write_text(source)
        (self.root/'evidence/evidence-index.yaml').write_text('EV-001:\n  path: evidence/EV-001.md\n  summary: 待讨论\n')
        a=obj(); a['provenance']['evidence_refs']=['EV-001']
        text,audit=self.run_compile(self.write([a]),query='连接池 原话')
        value=json.loads(text[text.index('{'):])
        self.assertEqual(value['evidence'][0]['source_text'],source)
        self.assertEqual(audit['evidence_loaded'],['EV-001'])
    def test_evidence_is_not_loaded_without_intent(self):
        text,audit=self.run_compile(self.write([obj()]))
        self.assertEqual(audit['evidence_loaded'],[])
    def test_no_type_prior_only_retrieval(self):
        base=self.write([obj(content='其他事情',retrieval={'keywords':['为什么'],'aliases':[]})])
        _,audit=self.run_compile(base,query='为什么')
        self.assertEqual(audit['ranking'],[])
    def test_deterministic_bytes_and_audit(self):
        base=self.write([obj(),obj('FACT-002')])
        first=self.run_compile(base); second=self.run_compile(Base(self.root))
        self.assertEqual(first,second)
    def test_source_hash_includes_evidence_bytes(self):
        (self.root/'evidence/EV-001.md').write_text('one')
        (self.root/'evidence/evidence-index.yaml').write_text('EV-001:\n  path: evidence/EV-001.md\n  summary: x\n')
        base=self.write([obj()]); old=base.source_hash
        (self.root/'evidence/EV-001.md').write_text('two')
        self.assertNotEqual(old,Base(self.root).source_hash)
    def test_cli_prevents_core_overwrite(self):
        self.write([obj()])
        result=subprocess.run([sys.executable,'-m','cch','--base',str(self.root),'context','--domain','test','--project','p','--as-of','2026-09-07','--query','连接池','--output',str(self.root/'cognitive-base/0.yaml')],capture_output=True,text=True)
        self.assertEqual(result.returncode,2); self.assertIn('WRITE_PROTECTED',result.stderr)
    def test_tokenizer_missing_cache_fails_without_network(self):
        with patch.dict('os.environ',{'TIKTOKEN_CACHE_DIR':str(self.root/'empty-cache')}):
            with patch('requests.get',side_effect=AssertionError('network forbidden')):
                with self.assertRaises(CCHError) as exc:
                    TokenCounter('cl100k_base')
                self.assertEqual(exc.exception.code,'TOKENIZER_UNAVAILABLE')
    def test_nested_field_allowlist(self):
        a=obj(); a['scope']['tags']='hidden-scope'; a['lifecycle']['tags']='hidden-life'
        text,_=self.run_compile(self.write([a]))
        self.assertNotIn('hidden-scope',text); self.assertNotIn('hidden-life',text)
    def test_synthetic_example_validates(self):
        base=Base(Path(__file__).resolve().parents[1]/'examples/demo-project')
        self.assertEqual(len(base.objects),40)

if __name__=='__main__': unittest.main()
