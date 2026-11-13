"""Header preservation and search presentation boundaries, without live providers."""
import csv
import io

import pytest

from web_search_mcp import inspection, search_helpers as helper


@pytest.mark.parametrize('content,expected',[
    ('name,value\nA,1\nB,2\n','present'),
    ('5.1,3.5,1.4,0.2,Iris-setosa\n4.9,3.0,1.4,0.2,Iris-setosa\n4.7,3.2,1.3,0.2,Iris-setosa\n','absent'),
    ('name,city\nAlice,London\nBob,Paris\n',None),
    ('1,score\n2,3\n4,5\n',None),
    ('id,value\n1,2\n',None),
    ('Alice,N/A\nBob,42\nCarol,39\n',None),
    ('Region,2024,2025\nNorth,10,20\nSouth,30,40\n',None),
    ('Region,2024.0,2025.0\nNorth,10,20\nSouth,30,40\n',None),
    ('1,2\n3,4\n5,6\n',None),
    ('name,score\nAlice,1\nBob,2\n','present'),
])
def test_header_detection_is_conservative(content,expected):
    decision=inspection.detect_header(inspection.text_rows(content.encode()))
    assert decision['header']==expected
    assert bool(decision['header_message'])


def test_headerless_csv_inspection_preserves_all_150_records(tmp_path,monkeypatch):
    from config import settings as cfg
    monkeypatch.setattr(cfg,'STAGING',tmp_path)
    content=''.join(f'{i},3.5,1.4,0.2,Iris-setosa\n' for i in range(150)).encode()
    active=inspection.inspect_download(content,'CSV','https://example.org/iris.csv')
    try:
        item=next(iter(active['files'].values()))
        assert item['header']=='absent' and item['row_count']==150
        (data,name,warnings),_=inspection.read_selected(active,item['id'],item['header'])
        rows=list(csv.reader(io.StringIO(data.decode())))
        assert len(rows)==151 and rows[1][0]=='0' and rows[-1][0]=='149'
        assert rows[0][0]=='column_1' and warnings
        # The explicit override is still honored for source files with unusual headers.
        (data,_,_),_=inspection.read_selected(active,item['id'],'present')
        assert len(list(csv.reader(io.StringIO(data.decode()))))==150
    finally: inspection.cleanup(active)
    assert not list(tmp_path.iterdir())


def test_uncertain_file_requires_explicit_header_and_supports_both_choices(tmp_path,monkeypatch):
    from config import settings as cfg
    monkeypatch.setattr(cfg,'STAGING',tmp_path)
    active=inspection.inspect_download(b'name,city\nAlice,London\nBob,Paris\n','CSV','https://example.org/cities.csv')
    try:
        item=next(iter(active['files'].values()))
        assert item['header'] is None and item['header_detection']=='uncertain'
        with pytest.raises(ValueError,match='first row'):
            inspection.read_selected(active,item['id'],None)
        for header,expected in [('present',3),('absent',4)]:
            (data,_,_),_=inspection.read_selected(active,item['id'],header)
            assert len(list(csv.reader(io.StringIO(data.decode()))))==expected
    finally: inspection.cleanup(active)


def test_dataset_sources_outrank_tutorials_without_removing_pages():
    rows=[{'name':'How to import Iris dataset tutorial','description':'Iris dataset CSV',
           'url':'https://example.org/tutorial','_tavily_score':.99},
          {'name':'Iris dataset','description':'Iris CSV samples',
           'url':'https://archive.ics.uci.edu/dataset/53/iris','_tavily_score':.7},
          {'name':'Iris dataset CSV','url':'https://example.org/iris.csv','_tavily_score':.6}]
    ranked=helper._rank_dataset_rows(rows,'Iris')
    assert ranked[0]['url'].endswith('iris.csv')
    assert ranked[1]['url'].startswith('https://archive.ics.uci.edu')
    prepared=helper.prepare_candidates(ranked)['candidates']
    assert len(prepared)==3
    assert prepared[0]['availability']=='direct_unverified'
    assert prepared[1]['availability']=='page_unverified'


def test_descriptions_remove_widgets_errors_and_history_without_inventing_facts():
    raw=('Download Course MIT OpenCourseWare search GIVE NOW help & faqs '
         'Iris dataset contains 150 samples and 5 columns. get_app grid_3x3 keyboard_arrow_down '
         'Sorry, something went wrong. There was an error while loading. Please reload this page. '
         '| date | Downloads | --- | 2026 | 123 |')
    cleaned=helper.clean_description(raw)
    assert '150 samples and 5 columns' in cleaned
    for noise in ['GIVE NOW','grid','Downloads','loading','reload','2026']:
        assert noise not in cleaned
    assert helper.clean_description('GIVE NOW contact us')=='Description unavailable. Inspect available files to review this source.'


@pytest.mark.parametrize('text,expected',[
    ('-10 to 40 degrees Celsius temperature measurements.', '-10 to 40'),
    ('Measurements < 5 and > 1 are included in this dataset.', '< 5 and > 1'),
    ('Measurements &lt; 5 and &gt; 1 in this dataset.', '< 5 and > 1'),
    ('Dataset measurements satisfy x<y and y>0 in all rows.', 'x<y and y>0'),
    ('Dataset measurements satisfy a<b and b>0 in all rows.', 'a<b and b>0'),
    ('Dataset measurements satisfy x<i and i>0 in all rows.', 'x<i and i>0'),
    ('Dataset measurements satisfy a<b>0 in all rows.', 'a<b>0'),
    ('<p>Dataset <b>measurements a<b and b>0</b> are included.</p>', 'a<b and b>0'),
    ('.5 to 1.5 measurements are included in this dataset.', '.5 to 1.5'),
    ('<p>Dataset contains <b>150</b> rows with x<y and y>0.</p>', 'x<y and y>0'),
    ('<p>Dataset contains <b>~150</b> samples in total.</p>', '~150'),
    ('The dataset has 3 product_id columns and ~150 samples.', 'product_id'),
])
def test_description_preserves_meaningful_symbols(text,expected):
    result=helper.clean_description(text)
    assert expected in result
    assert '<p>' not in result


def test_description_cleans_clear_html_with_attributes_and_line_breaks():
    assert helper.clean_description('<p class="summary">Dataset contains <b>150</b><br/> samples in total.</p>') == 'Dataset contains 150 samples in total.'


@pytest.mark.parametrize('filename,content,header,expected',[
    ('missing.csv',b'Alice,N/A\nBob,42\nCarol,39\n','absent',
     [['column_1','column_2'],['Alice','N/A'],['Bob','42'],['Carol','39']]),
    ('years.data',b'Region,2024,2025\nNorth,10,20\nSouth,30,40\n','present',
     [['Region','2024','2025'],['North','10','20'],['South','30','40']]),
])
def test_ambiguous_header_selection_applies_to_download_and_ingestion(tmp_path,monkeypatch,filename,content,header,expected):
    import asyncio
    from app import create_app
    from config import settings as cfg
    from shared import storage
    from web_search_mcp import dataset_search
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    monkeypatch.setattr(cfg,'UPLOADS',tmp_path/'uploads')
    url='https://example.org/'+filename
    async def search(*args):
        return {'candidates':[{'id':'c1','name':'Example','format':filename.rsplit('.',1)[-1].upper(),
                               'page_url':url,'url':url,'download_url':url,'file_name':filename}],
                'total_search_hits':1,'warnings':[]}
    async def fetch(source,*,page=False): return content,source
    monkeypatch.setattr(dataset_search,'search',search)
    monkeypatch.setattr(inspection,'fetch',fetch)
    class Bridge:
        uploaded=None
        def run(self,coro,timeout=320): return asyncio.run(coro)
        async def call_async(self,name,args):
            assert name=='rag_load_dataset'
            path,_=storage.resolve_source(args['source_ref'])
            self.uploaded=path.read_bytes()
            return {'dataset_id':'new','name':'Example','rows':len(expected)-1}
    bridge=Bridge();client=create_app(bridge).test_client()
    found=client.post('/dataset-search',json={'topic':'Example'}).get_json()
    selection={'search_id':found['search_id'],'candidate_id':'c1'}
    def inspect_file():
        response=client.post('/dataset-search/inspect',json=selection)
        assert response.status_code==200
        result=response.get_json(); item=result['files'][0]
        assert item['header'] is None and item['header_detection']=='uncertain'
        return {**selection,'inspection_id':result['inspection_id'],'file_id':item['id']}
    for endpoint in ['/dataset-search/download','/dataset-search/use']:
        assert client.post(endpoint,json=inspect_file()).status_code==400
        assert bridge.uploaded is None
    downloaded=client.post('/dataset-search/download',json={**inspect_file(),'header':header})
    assert downloaded.status_code==200 and bridge.uploaded is None
    assert list(csv.reader(io.StringIO(downloaded.data.decode())))==expected
    imported=client.post('/dataset-search/use',json={**inspect_file(),'header':header})
    assert imported.status_code==200
    assert list(csv.reader(io.StringIO(bridge.uploaded.decode())))==expected
    assert not list(cfg.STAGING.iterdir())


def test_equivalent_pages_deduplicate_without_rewriting_urls_or_losing_formats():
    rows=[{'name':'Iris dataset','url':'https://example.org/iris#files'},
          {'name':'Iris dataset','url':'https://example.org/iris#overview'},
          *[{'name':'Iris dataset','url':f'https://example.org/iris.{ext}'} for ext in ['csv','xlsx','zip','data']]]
    candidates=helper.prepare_candidates(rows)['candidates']
    assert len(candidates)==5 and candidates[0]['page_url']==rows[0]['url']
    assert [x['format'] for x in candidates]==['PAGE','CSV','XLSX','ZIP','DATA']
