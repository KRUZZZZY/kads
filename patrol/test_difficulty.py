"""Bookkeeping safeguards for resumable difficulty routing (no model mocks as proof)."""
import importlib.util, pathlib, sys, unittest, tempfile, json
sys.path.insert(0,str(pathlib.Path(__file__).parent))
import difficulty as d

class DifficultyTest(unittest.TestCase):
 def test_report_sections_keep_multiline_verdict_evidence(self):
  text='# batch\n### Cluster 12 — title\nVerdict: REAL-UNTESTED\nEvidence: first\n\nsecond\n### Cluster 13 — next\nVerdict: FALSE-POSITIVE\n'
  blocks=list(d.sections(text))
  self.assertEqual([x[0] for x in blocks],[12,13])
  self.assertIn('second',blocks[0][1]);self.assertNotIn('next',blocks[0][1])
  self.assertEqual([d.verdict(x[1]) for x in blocks],['REAL-UNTESTED','FALSE-POSITIVE'])
 def test_easy_requires_context_and_resolved_source(self):
  a={'easy':{'noul':.99},'hard':{'noul':.01},'context':{'noul':.9}}
  self.assertEqual(d.label(a,True),'easy');self.assertEqual(d.label(a,False),'needs_context')
  a['context']['noul']=.7;self.assertEqual(d.label(a,True),'needs_context')
 def test_conflicting_or_incomplete_answers_never_route_easy(self):
  self.assertEqual(d.label({},True),'needs_context')
  self.assertEqual(d.label({'easy':{'noul':.99},'hard':{'noul':.8},'context':{'noul':.9}},True),'hard')
 def test_hard_suggestion_retains_missing_context(self):
  self.assertEqual(d.label({'easy':{'noul':.1},'hard':{'noul':.9},'context':{'noul':.2}},True),'hard')
 def test_queue_shards_do_not_duplicate_or_drop_items(self):
  inventory=[{'cluster':i,'text':'finding'} for i in range(74)]
  decisions=[{'cluster':i,'difficulty':'needs_context'} for i in range(74)]
  with tempfile.TemporaryDirectory() as tmp:
   out=pathlib.Path(tmp);d.queues(out,inventory,decisions)
   shards=sorted((out/'queue/needs_context').glob('*.jsonl'))
   self.assertEqual([len(d.rows(p)) for p in shards],[50,24])
   merged=[r['cluster'] for p in shards for r in d.rows(p)]
   self.assertEqual(merged,list(range(74)))
   d.queues(out,inventory,decisions[:1])
   self.assertEqual(len(list((out/'queue/needs_context').glob('*.jsonl'))),1)
   self.assertEqual(len(d.rows(out/'queue/needs_context.jsonl')),1)
 def test_hard_with_sufficient_context_remains_hard(self):
  self.assertEqual(d.label({'easy':{'noul':.1},'hard':{'noul':.9},'context':{'noul':.95}},True),'hard')
if __name__=='__main__':unittest.main()
