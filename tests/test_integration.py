"""Real HTTP aggregator + STDIO Pandas/RAG with local mock providers.

Run with DEMO_INTEGRATION=1 and the Postman provider simulation on port 4010.
The source download boundary is redirected by this test to the fixture HTTP
server; the production public-URL guard is tested separately and is unchanged.
"""
import asyncio
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook
from openpyxl.worksheet.table import Table, TableStyleInfo

pytestmark=pytest.mark.skipif(os.getenv('DEMO_INTEGRATION')!='1',reason='Opt-in local provider/MCP integration run.')
ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def application(tmp_path_factory):
    from config import settings as cfg
    from app import create_app
    root=tmp_path_factory.mktemp('mcp-integration')
    original={name:getattr(cfg,name) for name in ['UPLOADS','STAGING','DATA_ROOT','AGGREGATOR_URL','LLM_URL','LLM_KEY']}
    cfg.DATA_ROOT=root
    cfg.UPLOADS=root/'uploads'
    cfg.STAGING=root/'temp_downloads'
    cfg.AGGREGATOR_URL='http://127.0.0.1:18000/mcp'
    cfg.LLM_URL='http://127.0.0.1:4010/providers/v1'
    cfg.LLM_KEY='fixture-only'
    env={**os.environ,'DEMO_DATA_ROOT':str(root),'DEMO_AGGREGATOR_PORT':'18000',
         'TAVILY_API_MCP_URL':'http://127.0.0.1:4010/providers/mcp','TAVILY_API_KEY':'fixture-only',
         'DEMO_OFFLINE_MODE':'1','DEMO_MAX_DATASETS':'4','PYTHONUNBUFFERED':'1','TOKENIZERS_PARALLELISM':'false'}
    log=(root/'aggregator.log').open('w',encoding='utf-8')
    process=subprocess.Popen([sys.executable,'-m','aggregator.aggregator_server'],cwd=ROOT,env=env,stdout=log,stderr=log)
    try:
        deadline=time.time()+90
        while True:
            if process.poll() is not None:
                pytest.fail('Aggregator stopped: '+(root/'aggregator.log').read_text(encoding='utf-8')[-3000:])
            try:
                with httpx.Client(timeout=2) as client:
                    response=client.get('http://127.0.0.1:18000/mcp')
                if response.status_code in {400,405,406}: break
            except httpx.HTTPError: pass
            if time.time()>deadline: pytest.fail('Aggregator startup timed out.')
            time.sleep(.3)
        app=create_app()
        app.config['TESTING']=True
        yield app,root
    finally:
        process.terminate()
        try: process.wait(timeout=8)
        except subprocess.TimeoutExpired: process.kill()
        log.close()
        for name,value in original.items(): setattr(cfg,name,value)


def test_full_two_ingestion_paths_scoped_chat_and_failure_preserves_selection(application,monkeypatch):
    app,root=application
    client=app.test_client()
    bridge=app.extensions['mcp_bridge']
    health=client.get('/health').get_json()
    assert health['local_servers']=={'rag':True,'pandas':True}
    fixture=b'Project,Task,Owner,Completion,survived,Planned,Actual\nBank,Debt API,A,0,1,2026-01-01,2026-01-03\nBank,Debt report,,50,0,2026-01-02,2026-01-02\nOther,Other task,B,100,1,2026-01-03,2026-01-02\n'
    response=client.post('/upload',data={'file':(io.BytesIO(fixture),'local.csv')})
    assert response.status_code==200,response.get_json()
    first=response.get_json()['dataset']
    assert first['rows']==3 and first['status']=='ready'
    assert (root/'uploads'/first['dataset_id']/'local.csv').is_file()
    assert not list((root/'temp_downloads').iterdir())
    count=bridge.call('aggregate_data',{'dataset_id':first['dataset_id'],'operation':'row_count',
                                      'filters':[{'column':'Project','operator':'eq','value':'Bank'}]})
    assert count['value']==2
    rag=bridge.call('rag_search_dataset',{'dataset_id':first['dataset_id'],'table_id':'data','query':'Debt tasks',
                                         'filters':[{'column':'Project','operator':'eq','value':'Bank'}]})
    assert rag['passages'] and all('Other task' not in p['text'] for p in rag['passages'])
    failed=client.post('/upload',data={'file':(io.BytesIO(b'a,b\n1\n'),'bad.csv')})
    assert failed.status_code==400,failed.get_json()
    assert client.get('/datasets').get_json()['selected_dataset_id']==first['dataset_id']
    search=client.post('/dataset-search',json={'topic':'Bank tasks','limit':5})
    assert search.status_code==200,search.get_json()
    results=search.get_json()
    assert len(results['candidates'])==1
    candidate=results['candidates'][0]
    assert candidate['download_url']=='https://raw.githubusercontent.com/example/demo/main/tasks.csv'
    from web_search_mcp import search_helpers
    async def fixture_download(url,settings):
        assert url==candidate['download_url']
        async with httpx.AsyncClient() as http:
            response=await http.get('http://127.0.0.1:4010/providers/dataset.csv')
            response.raise_for_status()
            return response.content
    monkeypatch.setattr(search_helpers,'download_csv',fixture_download)
    selection={'search_id':results['search_id'],'candidate_id':candidate['id']}
    downloaded=client.post('/dataset-search/download',json=selection)
    assert downloaded.status_code==200 and b'Debt API' in downloaded.data
    assert client.get('/datasets').get_json()['selected_dataset_id']==first['dataset_id']
    online=client.post('/dataset-search/use',json=selection)
    assert online.status_code==200,online.get_json()
    second=online.get_json()['dataset']
    assert first['dataset_id']!=second['dataset_id'] and second['rows']==3
    assert not list((root/'temp_downloads').iterdir())
    assert client.get('/datasets').get_json()['selected_dataset_id']==second['dataset_id']
    chat=client.post('/ask',json={'question':'Explain Debt tasks and count Bank tasks.','dataset_id':second['dataset_id']})
    assert chat.status_code==200,chat.get_json()
    result=chat.get_json()
    assert '2 tasks' in result['answer'] and len(result['tool_calls'])==3
    assert all(trace['arguments']['dataset_id']==second['dataset_id'] for trace in result['tool_calls'])
    assert {x['tool'] for x in result['tool_calls']}=={'get_dataset_info','aggregate_data','rag_search_dataset'}
    assert client.post('/ask',json={'question':'count','dataset_id':first['dataset_id']}).status_code==400
    assert client.post('/datasets/select',json={'dataset_id':first['dataset_id']}).status_code==200
    assert client.get('/chat/history').get_json()['messages']==[]
    workbook=Workbook()
    sheet=workbook.active;sheet.title='Tasks'
    for row in [['Project','Task','Completion %'],['Bank','Debt',.5],['Other','Other',0]]:sheet.append(row)
    sheet['C2'].number_format='0%';sheet['C3'].number_format='0%'
    sheet.add_table(Table(displayName='Work',ref='A1:C3'))
    content=io.BytesIO();workbook.save(content);content.seek(0)
    excel=client.post('/upload',data={'file':(content,'tasks.xlsx')})
    assert excel.status_code==200,excel.get_json()
    third=excel.get_json()['dataset']
    table_id=next(t['table_id'] for t in third['tables'] if t['role']=='records')
    mean=bridge.call('aggregate_data',{'dataset_id':third['dataset_id'],'table_id':table_id,'operation':'mean','column':'Completion %'})
    assert mean['value']==25 and not mean['warnings']
    # Only the fixture's loopback endpoint bypasses the production public-URL guard.
    monkeypatch.setattr(search_helpers,'_is_public_url',lambda url: None)
    found=client.post('/dataset-search',json={'topic':'Iris','limit':5})
    assert found.status_code==200,found.get_json()
    iris_search=found.get_json(); iris_candidate=iris_search['candidates'][0]
    assert iris_candidate['format']=='PAGE'
    iris_selection={'search_id':iris_search['search_id'],'candidate_id':iris_candidate['id']}
    inspected=client.post('/dataset-search/inspect',json=iris_selection)
    assert inspected.status_code==200,inspected.get_json()
    archive=inspected.get_json(); assert len(archive['files'])==2
    iris_file=next(item for item in archive['files'] if item['name']=='iris.data')
    imported=client.post('/dataset-search/use',json={**iris_selection,'inspection_id':archive['inspection_id'],
                                                     'file_id':iris_file['id'],'header':'absent'})
    assert imported.status_code==200,imported.get_json()
    fourth=imported.get_json()['dataset']
    assert fourth['rows']==150 and fourth['columns']==['column_1','column_2','column_3','column_4','column_5']
    assert fourth['source']['selected_file']=='iris.data'
    assert bridge.call('aggregate_data',{'dataset_id':fourth['dataset_id'],'operation':'row_count'})['value']==150
    assert not list((root/'temp_downloads').iterdir())
    assert client.post('/upload',data={'file':(io.BytesIO(fixture),'fifth.csv')}).status_code==400
    assert client.post('/dataset-search/use',json={'search_id':results['search_id'],'candidate_id':'wrong'}).status_code==400

