import asyncio
import csv
import io
import zipfile

import pytest

from config import settings as cfg
from shared import storage
from web_search_mcp import inspection, search_helpers


def archive(entries):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,content in entries:
            info=zipfile.ZipInfo(name); info.filename=name
            z.writestr(info,content)
    return out.getvalue()


def test_uci_page_archive_selection_preserves_headerless_rows(tmp_path,monkeypatch):
    from app import create_app
    from web_search_mcp import dataset_search
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    monkeypatch.setattr(cfg,'UPLOADS',tmp_path/'uploads')
    rows=''.join(f'{i},3.5,1.4,0.2,Iris-setosa\n' for i in range(150))
    payload=archive([('iris.data',rows),('bezdekIris.data',rows),('iris.names','metadata')])

    async def fake_search(bridge,topic,limit):
        return {'candidates':[{'id':'c1','name':'Iris','format':'PAGE','page_url':'https://example.org/iris',
                               'url':'https://example.org/iris','download_url':'','file_name':''}],
                'total_search_hits':1,'warnings':[]}
    async def fake_fetch(url,*,page=False):
        if page: return b'<a href="/iris.zip">Download</a>',url
        return payload,url
    monkeypatch.setattr(dataset_search,'search',fake_search)
    monkeypatch.setattr(inspection,'fetch',fake_fetch)

    class Bridge:
        def __init__(self): self.content=None; self.source=None
        def run(self,coro,timeout=320): return asyncio.run(coro)
        async def call_async(self,name,args):
            assert name=='rag_load_dataset'
            path,metadata=storage.resolve_source(args['source_ref'])
            self.content=path.read_bytes(); self.source=metadata['source']
            return {'dataset_id':'new','name':'Iris','rows':150}
    bridge=Bridge(); app=create_app(bridge); app.testing=True; client=app.test_client()
    search=client.post('/dataset-search',json={'topic':'Iris','limit':5}).get_json()
    selection={'search_id':search['search_id'],'candidate_id':'c1'}
    result=client.post('/dataset-search/inspect',json=selection)
    assert result.status_code==200,result.get_json()
    data=result.get_json(); assert len(data['files'])==2
    iris=next(x for x in data['files'] if x['name']=='iris.data')
    assert iris['header']=='absent' and iris['preview'][0][0]=='0'
    cancelled=client.post('/dataset-search/cancel-inspection',json={'inspection_id':data['inspection_id']})
    assert cancelled.status_code==200 and not list(cfg.STAGING.iterdir())
    data=client.post('/dataset-search/inspect',json=selection).get_json()
    iris=next(x for x in data['files'] if x['name']=='iris.data')
    downloaded=client.post('/dataset-search/download',json={**selection,'inspection_id':data['inspection_id'],
                                                            'file_id':iris['id'],'header':'absent'})
    assert downloaded.status_code==200 and downloaded.data.startswith(b'column_1,column_2')
    assert bridge.content is None and not list(cfg.STAGING.iterdir())
    data=client.post('/dataset-search/inspect',json=selection).get_json()
    iris=next(x for x in data['files'] if x['name']=='iris.data')
    response=client.post('/dataset-search/use',json={**selection,'inspection_id':data['inspection_id'],
                                                      'file_id':iris['id'],'header':'absent'})
    assert response.status_code==200,response.get_json()
    records=list(csv.reader(io.StringIO(bridge.content.decode())))
    assert records[0]==['column_1','column_2','column_3','column_4','column_5']
    assert len(records)==151 and records[1][0]=='0'
    assert bridge.source['selected_file']=='iris.data'
    assert not list(cfg.STAGING.iterdir())


@pytest.mark.parametrize('bad_name',['../escape.csv','/absolute.csv','C:/escape.csv','folder\\escape.csv'])
def test_unsafe_archive_paths_are_rejected_and_cleaned(tmp_path,monkeypatch,bad_name):
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    async def fake_fetch(url,*,page=False): return archive([(bad_name,'a,b\n1,2\n')]),url
    monkeypatch.setattr(inspection,'fetch',fake_fetch)
    with pytest.raises(ValueError,match='unsafe'):
        asyncio.run(inspection.inspect('https://example.org/archive.zip'))
    assert not list(cfg.STAGING.iterdir())


def test_candidate_recognizes_zip_xlsx_and_dataset_page():
    rows=[{'name':'Iris dataset','url':'https://example.org/iris'},
          {'name':'Archive','url':'https://example.org/data.zip'},
          {'name':'Workbook','url':'https://example.org/data.xlsx'}]
    result=search_helpers.prepare_candidates(rows)
    assert [item['format'] for item in result['candidates']]==['PAGE','ZIP','XLSX']


def test_text_validation_rejects_inconsistent_records():
    with pytest.raises(ValueError,match='inconsistent'):
        inspection.normalized(b'1,2\n3\n','iris.data','absent')


def test_xlsx_in_archive_is_preserved(tmp_path,monkeypatch):
    from openpyxl import Workbook
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    book=Workbook(); book.active.append(['name','value']); book.active.append(['a',1])
    content=io.BytesIO(); book.save(content)
    async def fake_fetch(url,*,page=False): return archive([('tables/data.xlsx',content.getvalue())]),url
    monkeypatch.setattr(inspection,'fetch',fake_fetch)
    result=asyncio.run(inspection.inspect('https://example.org/archive.zip'))
    item=next(iter(result['files'].values()))
    (exported,filename,_),_=inspection.read_selected(result,item['id'],'present')
    assert filename=='data.xlsx' and exported==content.getvalue()
    inspection.cleanup(result)
    assert not list(cfg.STAGING.iterdir())


def test_archive_expanded_limit_and_nested_zip_are_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    monkeypatch.setattr(cfg,'MAX_EXPANDED_BYTES',8)
    async def fake_fetch(url,*,page=False): return archive([('data.csv','name,value\na,123\n')]),url
    monkeypatch.setattr(inspection,'fetch',fake_fetch)
    with pytest.raises(ValueError,match='extraction limits'):
        asyncio.run(inspection.inspect('https://example.org/archive.zip'))
    monkeypatch.setattr(cfg,'MAX_EXPANDED_BYTES',100000)
    async def nested_fetch(url,*,page=False): return archive([('inside.zip','data')]),url
    monkeypatch.setattr(inspection,'fetch',nested_fetch)
    with pytest.raises(ValueError,match='Nested'):
        asyncio.run(inspection.inspect('https://example.org/archive.zip'))
    assert not list(cfg.STAGING.iterdir())


def test_unknown_rag_completion_keeps_only_staged_file(tmp_path,monkeypatch):
    from app import create_app
    from agent.mcp_client import UnknownCompletion
    from web_search_mcp import dataset_search
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    monkeypatch.setattr(cfg,'UPLOADS',tmp_path/'uploads')
    async def fake_search(bridge,topic,limit):
        return {'candidates':[{'id':'c1','name':'Iris','format':'ZIP','page_url':'https://example.org/iris',
                               'url':'https://example.org/iris','download_url':'https://example.org/iris.zip','file_name':'iris.zip'}],
                'total_search_hits':1,'warnings':[]}
    async def fake_fetch(url,*,page=False): return archive([('iris.data','1,2\n3,4\n')]),url
    monkeypatch.setattr(dataset_search,'search',fake_search)
    monkeypatch.setattr(inspection,'fetch',fake_fetch)
    class Bridge:
        def run(self,coro,timeout=320): return asyncio.run(coro)
        async def call_async(self,name,args): raise UnknownCompletion('unknown')
    app=create_app(Bridge()); app.testing=True; client=app.test_client()
    search=client.post('/dataset-search',json={'topic':'Iris'}).get_json()
    selection={'search_id':search['search_id'],'candidate_id':'c1'}
    info=client.post('/dataset-search/inspect',json=selection).get_json()
    response=client.post('/dataset-search/use',json={**selection,'inspection_id':info['inspection_id'],
                                                      'file_id':info['files'][0]['id'],'header':'absent'})
    assert response.status_code==504 and response.get_json()['unknown_completion']
    folders=list(cfg.STAGING.iterdir())
    assert len(folders)==1 and not folders[0].name.startswith('inspect-')
    assert (folders[0]/'iris.csv').is_file()
