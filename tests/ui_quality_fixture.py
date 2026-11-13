"""Disposable browser fixture: real Flask/UI/normalization; fixed search/files.

Run manually with python tests/ui_quality_fixture.py. No provider calls or keys.
Separate storage and cookie name keep the running demo's session/data intact.
"""
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from app import create_app
from config import settings as cfg
from pandas_mcp import analytics
from rag_mcp import ingestion
from web_search_mcp import inspection


class Model:
    tokenizer=None
    def encode(self,texts,**kwargs): return np.ones((len(texts),4),dtype=np.float32)


class Bridge:
    def run(self,coro,timeout=320): return asyncio.run(coro)
    def call(self,name,args):
        if name=='demo_status': return {'local_servers':{'pandas':True,'rag':True},'tavily_configured':True}
        if name=='rag_load_dataset': return ingestion.rag_load_dataset(**args)
        if name=='rag_get_dataset_info': return ingestion.rag_get_dataset_info(**args)
        if name=='filter_dataset': return analytics.filter_dataset(**args)
        raise ValueError('This browser fixture only supports preparation and preview.')
    async def call_async(self,name,args):
        if name=='tavily_search':
            records=[{'title':'Iris dataset CSV','url':'https://example.org/iris.csv','score':.8,
                      'content':'Iris dataset with 150 samples and 5 columns. get_app grid_3x3'},
                     {'title':'Cities dataset CSV','url':'https://example.org/cities.csv','score':.7,
                      'content':'Cities dataset with names and cities.'}]
            return {'structuredContent':{'results':records}}
        return self.call(name,args)


async def fetch(url,*,page=False):
    if url=='https://example.org/iris.csv':
        return ''.join(f'{i},3.5,1.4,0.2,Iris-setosa\n' for i in range(150)).encode(),url
    if url=='https://example.org/cities.csv': return b'name,city\nAlice,London\nBob,Paris\n',url
    raise ValueError('Unknown fixture URL.')


if __name__=='__main__':
    with TemporaryDirectory(prefix='quality-ui-',dir=cfg.ROOT/'runtime') as folder:
        cfg.DATA_ROOT=Path(folder);cfg.STAGING=Path(folder)/'staging';cfg.UPLOADS=Path(folder)/'uploads'
        inspection.fetch=fetch;ingestion.get_model=lambda:Model()
        app=create_app(Bridge());app.config['SESSION_COOKIE_NAME']='quality_review_session'
        app.run(host='127.0.0.1',port=15001,use_reloader=False)
