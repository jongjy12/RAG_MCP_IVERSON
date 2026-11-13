import io
from uuid import uuid4

import pandas as pd
import pytest

from config import settings as cfg
from pandas_mcp import analytics as a
from shared import storage
from rag_mcp.ingestion import validate_csv, convert_xls, validate_workbook
from web_search_mcp.search_helpers import _is_public_url, direct_csv_url


@pytest.fixture
def dataset(tmp_path,monkeypatch):
    monkeypatch.setattr(cfg,'UPLOADS',tmp_path/'uploads')
    dataset_id=uuid4().hex
    folder=storage.dataset_dir(dataset_id)
    frame=pd.DataFrame({'Project':['Bank','Bank','Bank','Other'],
                        'Owner':['A',None,'B','C'], 'Completion':[0,50,None,100],
                        'survived':[1,0,1,0],
                        'Planned':['2026-01-01','2026-01-02','2026-01-03','01/02/2026'],
                        'Actual':['2026-01-03','2026-01-02','2026-01-02',None]})
    table={'id':'tasks','label':'Tasks','sheet':None,'frame':frame,'source_rows':[2,3,4,5],'role':'records','kind':'records','percentage_columns':['Completion']}
    summary=storage.save_table(folder,table)
    storage.atomic_json(folder/'dataset.json',{'dataset_id':dataset_id,'name':'Fixture','status':'ready','tables':[summary]})
    return dataset_id


def test_complete_filtered_counts_missing_and_real_zero(dataset):
    filters=[{'column':'Project','operator':'eq','value':'Bank'}]
    assert a.aggregate_data(dataset,'row_count',filters=filters)['value']==3
    assert a.aggregate_data(dataset,'count','Completion',filters)['value']==2
    mean=a.aggregate_data(dataset,'mean','Completion',filters)
    assert mean['value']==25 and mean['missing']==1 and mean['included']==2
    assert a.aggregate_data(dataset,'survival_rate','survived',filters)['denominator']==3
    assert a.filter_dataset(dataset,[{'column':'Owner','operator':'is_missing'}])['total_matches']==1


def test_group_missing_retained_and_sort_before_limit(dataset):
    result=a.groupby_data(dataset,['Owner'],[{'operation':'row_count','alias':'tasks'}],sort_by='tasks',limit=1)
    assert result['total_groups']==4 and result['truncated']
    all_groups=a.groupby_data(dataset,['Owner'],[{'column':'Completion','operation':'sum','alias':'progress'}])
    assert any(r['Owner'] is None and r['progress']==50 for r in all_groups['records'])


def test_date_signs_missing_and_ambiguous_dates(dataset):
    result=a.compare_dates(dataset,'Planned','Actual')
    assert {k:result[k] for k in ['late','on_time','early','invalid','missing']}==dict(late=1,on_time=1,early=1,invalid=1,missing=0)
    assert [r['days_difference'] for r in result['records'][:3]]==[2,0,-1]


def test_unsafe_filters_and_cross_dataset_paths_rejected(dataset):
    with pytest.raises(ValueError): a.filter_dataset(dataset,[{'column':'Owner','operator':'eval','value':'1+1'}])
    with pytest.raises(ValueError): a.aggregate_data(dataset,'mean','no_such_column')
    with pytest.raises(ValueError): storage.dataset_dir('../../outside')
    with pytest.raises(ValueError): _is_public_url('http://127.0.0.1/private.csv')


def test_csv_quotes_newlines_headers_and_invalid_content():
    data=b'name,name,\n"A, B","two\nlines",0\n'
    exported,warnings=validate_csv(data)
    assert b'name,name_2,Column 3' in exported and b'"two\nlines"' in exported and warnings
    for bad in [b'a,b\n',b'a,b\n1\n',b'<!DOCTYPE html><html>login</html>',b'a,b\n1,2,3\n']:
        with pytest.raises(ValueError): validate_csv(bad)
    assert direct_csv_url('https://github.com/org/repo/blob/main/a.csv')=='https://raw.githubusercontent.com/org/repo/main/a.csv'


def test_real_legacy_biff_xls_conversion_preserves_zero():
    from pathlib import Path
    from openpyxl import load_workbook
    content=convert_xls((Path(__file__).parent/'legacy_fixture.xls').read_bytes())
    validate_workbook(content)
    workbook=load_workbook(io.BytesIO(content),data_only=True)
    sheet=workbook.active
    assert sheet['A1'].value=='Task' and sheet['B2'].value==2 and sheet['B3'].value==0
    workbook.close()

