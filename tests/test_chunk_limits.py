"""Exercise the ingestion boundary without computing thousands of embeddings."""
import asyncio
import numpy as np
import pytest

from config import settings as cfg
from pandas_mcp import analytics
from rag_mcp import ingestion
from shared import storage


@pytest.mark.parametrize('limit,count,accepted', [
    (15_000, 0, False),
    (15_000, 1, True),
    (10_000, 13_017, False),
    (15_000, 10_000, True),
    (15_000, 13_017, True),
    (15_000, 15_000, True),
    (15_000, 15_001, False),
])
def test_chunk_budget_is_enforced_before_embedding(tmp_path, monkeypatch, limit, count, accepted):
    monkeypatch.setattr(cfg, 'STAGING', tmp_path / 'staging')
    monkeypatch.setattr(cfg, 'UPLOADS', tmp_path / 'uploads')
    monkeypatch.setattr(cfg, 'MAX_CHUNKS', limit)
    monkeypatch.setattr(ingestion, '_indexes', {})

    class Model:
        tokenizer = None
        encoded = 0

        def encode(self, texts, **kwargs):
            self.encoded += len(texts)
            return np.ones((len(texts), 4), dtype=np.float32)

    model = Model()
    monkeypatch.setattr(ingestion, 'get_model', lambda: model)
    # Isolate the budget policy from tokenization. The live benchmark separately
    # exercises the real tokenizer, real embeddings, and the full source file.
    def chunks(tables, tokenizer):
        table = tables[0]
        return [{'table_id': table['id'], 'source_row': table['source_rows'][0],
                 'text': 'Product: Book'} for _ in range(count)]
    monkeypatch.setattr(ingestion.parser, 'build_chunks', chunks)
    source = storage.stage_file('products.csv', b'Product,Price\nBook,12\nPen,2\n')
    try:
        if not accepted:
            with pytest.raises(ValueError, match=f'1-{limit} searchable chunks'):
                ingestion.rag_load_dataset(source)
            assert model.encoded == 0
            assert not ingestion._indexes
            assert not list(cfg.UPLOADS.glob('*/dataset.json'))
        else:
            info = ingestion.rag_load_dataset(source)
            assert info['status'] == 'ready' and info['chunks'] == count
            assert info['rows'] == 2 and info['columns'] == ['Product', 'Price']
            assert model.encoded == count
            index, indexed = ingestion._indexes[info['dataset_id']]
            assert index.ntotal == len(indexed) == count
            assert analytics.aggregate_data(info['dataset_id'], 'row_count')['value'] == 2
            assert analytics.aggregate_data(info['dataset_id'], 'sum', 'Price')['value'] == 14
    finally:
        storage.cleanup_source(source)


@pytest.mark.parametrize('name,expected', [('rag_load_dataset', 920), ('rag_search_dataset', 300), ('aggregate_data', 300)])
def test_bridge_extends_only_ingestion_deadline(monkeypatch, name, expected):
    from agent import mcp_client
    captured = {}

    class Client:
        def __init__(self, url, **options): captured.update(options)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def call_tool(self, tool, arguments): return {'ok': True}

    monkeypatch.setattr(cfg, 'INGEST_TIMEOUT_SECONDS', 900)
    monkeypatch.setattr(mcp_client, 'Client', Client)
    monkeypatch.setattr(mcp_client, 'unpack', lambda result: result)
    assert asyncio.run(mcp_client.Bridge.call_async(None, name, {})) == {'ok': True}
    assert captured['timeout'] == expected and captured['init_timeout'] == 15


@pytest.mark.parametrize('name,expected', [('rag_load_dataset', {'timeout': 900}), ('rag_search_dataset', {}), ('aggregate_data', {})])
def test_aggregator_overrides_only_ingestion_deadline(monkeypatch, name, expected):
    from aggregator import aggregator_server
    captured = {}

    class Client:
        async def call_tool(self, tool, arguments, **options):
            captured.update(options)
            return {'ok': True}

    monkeypatch.setattr(cfg, 'INGEST_TIMEOUT_SECONDS', 900)
    monkeypatch.setattr(aggregator_server, 'clients', {'fixture': Client()})
    monkeypatch.setattr(aggregator_server, 'unpack', lambda result: result)
    assert asyncio.run(aggregator_server.call('fixture', name, {})) == {'ok': True}
    assert captured == expected


def test_ingestion_timeout_still_reports_unknown_completion(monkeypatch):
    from agent import mcp_client
    calls = []

    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def call_tool(self, tool, arguments):
            calls.append(tool)
            raise TimeoutError('Fixture transport timed out after sending the request.')

    monkeypatch.setattr(mcp_client, 'Client', Client)
    with pytest.raises(mcp_client.UnknownCompletion):
        asyncio.run(mcp_client.Bridge.call_async(None, 'rag_load_dataset', {}))
    assert calls == ['rag_load_dataset']
