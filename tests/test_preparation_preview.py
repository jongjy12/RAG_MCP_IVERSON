"""Local preparation/preview boundaries; fake embeddings, no provider calls."""
import asyncio
import io
from concurrent.futures import TimeoutError

import numpy as np
import pandas as pd
import pytest

from app import create_app
from config import settings as cfg
from pandas_mcp import analytics
from rag_mcp import ingestion
from shared import progress, storage


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'DATA_ROOT', tmp_path)
    monkeypatch.setattr(cfg, 'STAGING', tmp_path / 'staging')
    monkeypatch.setattr(cfg, 'UPLOADS', tmp_path / 'uploads')
    monkeypatch.setattr(ingestion, '_indexes', {})

    class Model:
        tokenizer = None

        def encode(self, texts, **kwargs):
            return np.ones((len(texts), 4), dtype=np.float32)

    monkeypatch.setattr(ingestion, 'get_model', lambda: Model())

    class Bridge:
        calls = None

        def __init__(self): self.calls = []
        def run(self, coro, timeout=320): return asyncio.run(coro)
        async def call_async(self, name, args): return self.call(name, args)
        def call(self, name, args):
            self.calls.append((name, args))
            if name == 'rag_load_dataset': return ingestion.rag_load_dataset(**args)
            if name == 'rag_get_dataset_info': return ingestion.rag_get_dataset_info(**args)
            if name == 'filter_dataset': return analytics.filter_dataset(**args)
            raise AssertionError(name)

    bridge = Bridge()
    app = create_app(bridge)
    app.testing = True
    return app, bridge


def start(client, kind='upload'):
    response = client.post('/preparation/start', json={'kind': kind})
    assert response.status_code == 200
    return response.get_json()['operation_id']


def test_preparation_preview_session_scope_and_no_duplicate_ingestion(application, monkeypatch):
    app, bridge = application
    client = app.test_client()
    op = start(client)
    stages = []
    original = progress.update

    def capture(operation_id, stage, state='running'):
        if operation_id == op: stages.append(stage)
        original(operation_id, stage, state)

    monkeypatch.setattr(progress, 'update', capture)
    data = b'Product,Price\n' + b''.join(f'Item {i},{i}\n'.encode() for i in range(12))
    response = client.post('/upload', data={'operation_id': op, 'file': (io.BytesIO(data), 'products.csv')})
    assert response.status_code == 200, response.get_json()
    info = response.get_json()['dataset']
    before = len(bridge.calls)
    status = client.get('/preparation/' + op).get_json()
    assert status['state'] == 'complete' and status['stage'] == 'Dataset ready'
    assert len(bridge.calls) == before  # Polling doesn't compete for MCP.
    assert 'Reading and validating data' in stages
    assert any(s.startswith('Creating embeddings') for s in stages)
    assert 'Building search index and saving data' in stages
    table_id = info['tables'][0]['table_id']
    url = f"/datasets/preview?dataset_id={info['dataset_id']}&table_id={table_id}"
    preview = client.get(url).get_json()
    assert preview['total_matches'] == 12 and len(preview['records']) == 10
    assert preview['records'][0] == {'Product': 'Item 0', 'Price': 0}
    assert bridge.calls[-1] == ('filter_dataset', {'dataset_id': info['dataset_id'], 'table_id': table_id, 'limit': 10})
    assert client.get(f"/datasets/preview?dataset_id={info['dataset_id']}&table_id=invalid-table").status_code == 400
    stranger = app.test_client()
    assert stranger.get('/preparation/' + op).status_code == 404
    assert stranger.get(url).status_code == 400
    # Explicit table selection uses the same stored records, without re-parsing.
    folder=storage.dataset_dir(info['dataset_id'])
    table={'id':'second','label':'Second sheet','sheet':'Second','frame':pd.DataFrame({'Value':[7,9]}),
           'source_rows':[2,3],'role':'records','kind':'records','percentage_columns':[]}
    info['tables'].append(storage.save_table(folder,table))
    storage.atomic_json(folder/'dataset.json',info)
    second=client.get(f"/datasets/preview?dataset_id={info['dataset_id']}&table_id=second").get_json()
    assert second['total_matches']==2 and second['records']==[{'Value':7},{'Value':9}]
    duplicate = client.post('/upload', data={'operation_id': op, 'file': (io.BytesIO(data), 'products.csv')})
    assert duplicate.status_code == 400
    assert sum(name == 'rag_load_dataset' for name, _ in bridge.calls) == 1


def test_failed_upload_preserves_selection_and_marks_failure(application):
    app, _ = application
    client = app.test_client()
    # Existing API callers can still omit operation_id.
    first = client.post('/upload', data={'file': (io.BytesIO(b'a,b\n1,2\n'), 'good.csv')}).get_json()['dataset']
    op = start(client)
    bad = client.post('/upload', data={'operation_id': op, 'file': (io.BytesIO(b'a,b\n1\n'), 'bad.csv')})
    assert bad.status_code == 400
    assert client.get('/preparation/' + op).get_json()['state'] == 'failed'
    assert client.get('/datasets').get_json()['selected_dataset_id'] == first['dataset_id']
    assert not list(cfg.STAGING.iterdir())


def test_unknown_completion_is_reported_without_retry(application, monkeypatch):
    app, bridge = application
    client = app.test_client()
    op = start(client)
    calls = []

    def timeout(coro, timeout=320):
        calls.append('attempt')
        coro.close()
        raise TimeoutError()

    monkeypatch.setattr(bridge, 'run', timeout)
    response = client.post('/upload', data={'operation_id': op, 'file': (io.BytesIO(b'a,b\n1,2\n'), 'file.csv')})
    assert response.status_code == 504 and response.get_json()['unknown_completion']
    assert client.get('/preparation/' + op).get_json()['state'] == 'unknown'
    progress.update(op, 'A late background embedding stage')
    assert client.get('/preparation/' + op).get_json()['state'] == 'unknown'
    assert calls == ['attempt']


def test_operations_are_owned_typed_and_single_use(application):
    app, _ = application
    owner, stranger = app.test_client(), app.test_client()
    op = start(owner, 'inspect')
    for client in [stranger, owner]:
        result = client.post('/upload', data={'operation_id': op, 'file': (io.BytesIO(b'a,b\n1,2\n'), 'file.csv')})
        assert result.status_code == 400
    assert owner.get('/preparation/' + op).get_json()['state'] == 'running'
