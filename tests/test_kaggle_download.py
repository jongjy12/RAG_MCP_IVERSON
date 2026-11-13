import asyncio
import csv
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from config import settings as cfg
from web_search_mcp import inspection, kaggle_download, kaggle_worker, search_helpers

PAGE='https://www.kaggle.com/datasets/karkavelrajaj/amazon-sales-dataset'


@pytest.mark.parametrize('url,expected',[
    (PAGE,'karkavelrajaj/amazon-sales-dataset'),
    (PAGE+'?select=amazon.csv','karkavelrajaj/amazon-sales-dataset'),
    (PAGE+'/versions/3','karkavelrajaj/amazon-sales-dataset/3'),
    ('https://kaggle.com.evil.test/datasets/owner/data',None),
    ('https://www.kaggle.com/competitions/titanic',None),
    ('https://www.kaggle.com/datasets/owner/%2e%2e',None),
])
def test_dataset_reference(url,expected):
    assert kaggle_download.dataset_reference(url)==expected


def test_preview_query_is_a_page_not_csv():
    candidate=search_helpers.prepare_candidates([{'name':'Amazon dataset','url':PAGE+'?select=amazon.csv'}])['candidates'][0]
    assert candidate['format']=='PAGE' and not candidate['download_url']
    with pytest.raises(ValueError,match='HTTPS'):
        kaggle_download.dataset_reference(PAGE.replace('https:','http:'))


def test_missing_token_does_not_start_a_download(tmp_path,monkeypatch):
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    monkeypatch.delenv('KAGGLE_API_TOKEN',raising=False)
    with pytest.raises(ValueError,match='require KAGGLE_API_TOKEN'):
        asyncio.run(kaggle_download.inspect_dataset(PAGE))
    assert not cfg.STAGING.exists()


def test_kaggle_selection_export_and_ingestion_cleanup(tmp_path,monkeypatch):
    from app import create_app
    from shared import storage
    from web_search_mcp import dataset_search
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    monkeypatch.setattr(cfg,'UPLOADS',tmp_path/'uploads')
    monkeypatch.setenv('KAGGLE_API_TOKEN','fixture-secret')
    async def fake_search(*args):
        candidate=search_helpers.prepare_candidates([{'name':'Amazon dataset','url':PAGE+'?select=amazon.csv'}])['candidates'][0]
        return {'candidates':[candidate],'total_search_hits':1,'warnings':[]}
    async def fake_worker(reference,folder,token):
        assert reference=='karkavelrajaj/amazon-sales-dataset' and token=='fixture-secret'
        with zipfile.ZipFile(Path(folder)/'amazon-sales-dataset.zip','w') as archive:
            archive.writestr('amazon.csv','product,price\nBook,12\nPen,2\n')
            archive.writestr('other.csv','id,value\n1,2\n')
            archive.writestr('readme.txt','metadata')
    monkeypatch.setattr(dataset_search,'search',fake_search)
    monkeypatch.setattr(kaggle_download,'run_worker',fake_worker)
    class Bridge:
        source=None
        def run(self,coro,timeout=320): return asyncio.run(coro)
        async def call_async(self,name,args):
            assert name=='rag_load_dataset'
            path,metadata=storage.resolve_source(args['source_ref'])
            assert len(list(csv.reader(io.StringIO(path.read_text()))))==3
            self.source=metadata['source']
            return {'dataset_id':'new','name':'Amazon','rows':2}
    bridge=Bridge(); app=create_app(bridge); app.testing=True; client=app.test_client()
    search=client.post('/dataset-search',json={'topic':'Amazon'}).get_json()
    selection={'search_id':search['search_id'],'candidate_id':search['candidates'][0]['id']}
    def inspect_file():
        response=client.post('/dataset-search/inspect',json=selection)
        assert response.status_code==200,response.get_json()
        data=response.get_json()
        assert 'fixture-secret' not in response.get_data(as_text=True)
        assert len(data['files'])==2 and len(list(cfg.STAGING.iterdir()))==1
        item=next(x for x in data['files'] if x['name']=='amazon.csv')
        return {**selection,'inspection_id':data['inspection_id'],'file_id':item['id'],'header':'present'}
    chosen=inspect_file()
    assert client.post('/dataset-search/cancel-inspection',json=chosen).status_code==200
    assert not list(cfg.STAGING.iterdir())
    chosen=inspect_file()
    response=client.post('/dataset-search/download',json=chosen)
    assert response.status_code==200 and b'Book,12' in response.data and bridge.source is None
    assert not list(cfg.STAGING.iterdir())
    chosen=inspect_file()
    response=client.post('/dataset-search/use',json=chosen)
    assert response.status_code==200,response.get_json()
    assert bridge.source['page_url']==PAGE+'?select=amazon.csv'
    assert bridge.source['download_url']==PAGE
    assert bridge.source['provider']=='kaggle' and bridge.source['selected_file']=='amazon.csv'
    assert bridge.source['dataset_ref']=='karkavelrajaj/amazon-sales-dataset'
    assert 'fixture-secret' not in json.dumps(bridge.source)
    assert not list(cfg.STAGING.iterdir())


def test_worker_timeout_and_partial_file_cleanup(tmp_path,monkeypatch):
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    monkeypatch.setenv('KAGGLE_API_TOKEN','fixture-secret')
    worker=tmp_path/'slow.py'
    worker.write_text('import sys,time\nfrom pathlib import Path\nwith (Path(sys.argv[2])/"partial.zip").open("wb") as f:\n f.write(b"partial"); f.flush(); time.sleep(60)\n')
    monkeypatch.setattr(kaggle_download,'WORKER',worker)
    monkeypatch.setattr(kaggle_download,'DOWNLOAD_TIMEOUT',0.5)
    with pytest.raises(ValueError,match='timed out'):
        asyncio.run(kaggle_download.inspect_dataset(PAGE))
    assert not list(cfg.STAGING.iterdir())


@pytest.mark.parametrize('code',['auth','forbidden','rate_limit','not_found','dependency','download'])
def test_sanitized_worker_errors_and_cleanup(tmp_path,monkeypatch,code):
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    monkeypatch.setenv('KAGGLE_API_TOKEN','fixture-secret')
    worker=tmp_path/'error.py'
    worker.write_text('import sys\nprint('+repr(json.dumps({'error':code,'detail':'fixture-secret'}))+')\nsys.exit(1)\n')
    monkeypatch.setattr(kaggle_download,'WORKER',worker)
    with pytest.raises(ValueError) as error:
        asyncio.run(kaggle_download.inspect_dataset(PAGE))
    assert str(error.value)==kaggle_download.ERRORS[code]
    assert 'fixture-secret' not in str(error.value) and not list(cfg.STAGING.iterdir())


def test_streaming_size_limit_without_content_length(tmp_path):
    class Response:
        headers={}
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def raise_for_status(self): pass
        def iter_content(self,chunk_size): return iter([b'12345',b'67890'])
    path=tmp_path/'large.zip'
    with pytest.raises(kaggle_worker.DownloadTooLarge):
        kaggle_worker.save_response(Response(),path,8)
    assert path.stat().st_size<=8


@pytest.mark.skipif(os.getenv('DEMO_INTEGRATION')!='1',reason='Requires local Postman simulation on 4010.')
@pytest.mark.parametrize('token,dataset,expected',[
    ('fixture-token','iris','ok'),('invalid','iris','auth'),
    ('fixture-token','forbidden','forbidden'),('fixture-token','rate-limited','rate_limit'),
])
def test_official_sdk_against_provider_simulation(tmp_path,monkeypatch,capsys,token,dataset,expected):
    import kagglesdk
    import kagglesdk.kaggle_env as sdk_env
    original=kagglesdk.KaggleClient
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'inspections')
    monkeypatch.setitem(sdk_env._env_to_endpoint,kagglesdk.KaggleEnv.LOCAL,'http://127.0.0.1:4010/providers')
    def local_client(**kwargs):
        kwargs['env']=kagglesdk.KaggleEnv.LOCAL
        return original(**kwargs)
    monkeypatch.setattr(kagglesdk,'KaggleClient',local_client)
    monkeypatch.setenv('KAGGLE_API_TOKEN',token)
    monkeypatch.setattr('sys.argv',['kaggle_worker.py','owner/'+dataset,str(tmp_path),str(cfg.MAX_BYTES)])
    result=kaggle_worker.main()
    status=json.loads(capsys.readouterr().out)
    assert status==({'status':'ok'} if expected=='ok' else {'error':expected})
    assert result==(0 if expected=='ok' else 1)
    if expected=='ok':
        active=inspection.inspect_download((tmp_path/'iris.zip').read_bytes(),'ZIP',PAGE)
        try: assert {x['name'] for x in active['files'].values()}=={'iris.data','bezdekIris.data'}
        finally: inspection.cleanup(active)
