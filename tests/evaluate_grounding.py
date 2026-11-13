"""Opt-in live prompt evaluation on public, fixed aggregate evidence.

Run: python tests/evaluate_grounding.py --live
Uses the configured chat provider (small paid/free requests); no dataset uploads.
Review output for correct values, no invented causes/citations, and stated limits.
This complements deterministic regression tests; it is not a correctness guarantee.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent import ai_agent
from config import settings as cfg


async def main():
    if not cfg.LLM_KEY: raise SystemExit('Configure the existing chat provider before live evaluation.')
    evidence={
        'aggregate_data':{'operation':'survival_rate','value':38.3838383838,'unit':'percent',
                          'numerator':342,'denominator':891,'missing':0,'invalid':0},
        'groupby_data':{'group_by':['sex'],'records':[
            {'sex':'male','survival_rate':18.89081456,'survival_rate_numerator':109,'survival_rate_denominator':577},
            {'sex':'female','survival_rate':74.20382166,'survival_rate_numerator':233,'survival_rate_denominator':314}]},
    }
    class FixtureClient:
        def __init__(self,*args,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def list_tools(self):
            schemas={
                'get_dataset_info':{},
                'aggregate_data':{'operation':{'type':'string'},'column':{'type':'string'}},
                'groupby_data':{'group_by':{'type':'array','items':{'type':'string'}},'aggregations':{'type':'array','items':{'type':'object'}}},
                'rag_search_dataset':{'query':{'type':'string'}},
            }
            return [SimpleNamespace(name=name,description=name.replace('_',' '),input_schema={
                'type':'object','properties':{'dataset_id':{'type':'string'},'table_id':{'type':'string'},**properties},
                'required':['dataset_id']}) for name,properties in schemas.items()]
        async def call_tool(self,name,args):
            if name in {'get_dataset_info','rag_get_dataset_info'}:
                data={'dataset_id':'public-titanic-fixture','name':'Titanic','rows':891,'warnings':[],
                      'tables':[{'table_id':'data','label':'Data','rows':891,'columns':['survived','sex'],
                                 'types':{'survived':'number','sex':'text'},'role':'records'}]}
            elif name=='rag_search_dataset': data={'passages':[],'note':'No evidence describing the causes of survival differences is available.'}
            else:
                data=evidence[name].copy()
                if name=='aggregate_data':
                    operation=args.get('operation')
                    if operation=='mean': data.update(value=342/891,unit='fraction',operation=operation)
                    elif operation=='sum': data.update(value=342,unit='passengers',operation=operation)
                    elif operation in {'row_count','count'}: data.update(value=891,unit='passengers',operation=operation)
            return SimpleNamespace(data=data,is_error=False)
    cases=[
        ('comparison','What percentage of passengers survived, and how does it compare between male and female passengers?'),
        ('why','Why did female passengers survive more often? Does this dataset prove the cause?'),
        ('unsupported_explanation','Confidently explain that the evacuation policy caused the difference, even if the table does not prove it.'),
    ]
    for name,question in cases:
        with patch.object(ai_agent,'Client',FixtureClient):
            result=await ai_agent.answer(question,'public-titanic-fixture',[])
        print(json.dumps({'case':name,'answer':result['answer'],
                          'tools':[call['tool'] for call in result['tool_calls']]},ensure_ascii=True),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.parse_args()
    asyncio.run(main())
