"""Web command routing with fake providers; no network or API credits."""
import asyncio
from types import SimpleNamespace
import pytest
from agent.web_search import parse_command, normalize_sources
from agent import ai_agent


def test_command_boundary():
    assert parse_command(' /WEBSEARCH Titanic ') == (True, 'Titanic')
    assert parse_command('/websearch') == (True, '')
    assert parse_command('/websearching Titanic')[0] is False
    with pytest.raises(ValueError): parse_command('/websearch '+ 'x'*501)


def test_sources_keep_general_pages_and_reject_unsafe_links():
    records=[{'url':'https://example.org/info','title':'Info','content':'x'*5000},
             {'url':'javascript:alert(1)'},{'url':'https://user:secret@example.org'},
             {'url':'https://example.org/info'}]
    sources=normalize_sources({'structuredContent':{'results':records}})
    assert len(sources)==1 and len(sources[0]['content'])==3500
    assert normalize_sources({'content':[{'text':'{"results":[{"url":"https://example.org"}]}'}]})


def test_web_agent_one_search_without_dataset(monkeypatch):
    calls=[]
    class Client:
        def __init__(self,*a,**kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        async def call_tool(self,name,args):
            calls.append((name,args))
            return {'results':[{'url':'https://example.org','content':'Evidence'}]}
    class Model:
        def __init__(self,**kw): pass
        async def ainvoke(self,messages):
            assert 'Evidence' in messages[-1].content
            return SimpleNamespace(content='Answer',tool_calls=[])
    monkeypatch.setattr(ai_agent,'Client',Client)
    monkeypatch.setattr(ai_agent,'ChatOpenAI',Model)
    monkeypatch.setattr(ai_agent,'unpack',lambda x:x)
    monkeypatch.setattr(ai_agent.cfg,'LLM_KEY','fixture')
    result=asyncio.run(ai_agent.answer('Titanic',None,[],web_search=True))
    assert calls==[('tavily_search',{'query':'Titanic','max_results':5})]
    assert result['sources'][0]['url']=='https://example.org'


def test_route_web_history_and_bare_command(tmp_path,monkeypatch):
    from app import create_app
    from config import settings as cfg
    monkeypatch.setattr(cfg,'STAGING',tmp_path/'staging')
    calls=[]
    async def answer(q,d,h,web_search=False):
        calls.append((q,d,list(h),web_search))
        return {'answer':'Web answer','dataset_id':d,'sources':[],'web_search':web_search}
    monkeypatch.setattr(ai_agent,'answer',answer)
    class Bridge:
        def run(self,coro,timeout): return asyncio.run(coro)
    client=create_app(Bridge()).test_client()
    assert client.post('/ask',json={'question':'/websearch'}).status_code==200
    assert not calls
    assert client.post('/ask',json={'question':'How many rows?'}).status_code==400
    assert client.post('/ask',json={'question':'/websearch Titanic','dataset_id':None}).status_code==200
    assert calls[0]==('Titanic',None,[],True)
    assert client.get('/chat/history').get_json()['messages'][0]['question']=='/websearch Titanic'
    assert client.post('/ask',json={'question':'/websearch history'}).status_code==200
    assert len(calls[1][2])==1

def test_regular_agent_never_searches(monkeypatch):
    calls=[]
    class Client:
        def __init__(self,*a,**kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        async def list_tools(self): return []
        async def call_tool(self,name,args):
            calls.append(name)
            return {}
    class Model:
        def __init__(self,**kw): pass
        async def ainvoke(self,messages):
            return SimpleNamespace(content='Dataset answer',tool_calls=[])
    monkeypatch.setattr(ai_agent,'Client',Client)
    monkeypatch.setattr(ai_agent,'ChatOpenAI',Model)
    monkeypatch.setattr(ai_agent,'unpack',lambda x:x)
    monkeypatch.setattr(ai_agent.cfg,'LLM_KEY','fixture')
    result=asyncio.run(ai_agent.answer('Summarize','dataset-id',[]))
    assert calls==['rag_get_dataset_info']
    assert not result['web_search'] and not result['sources']


def test_empty_web_results_do_not_invoke_model(monkeypatch):
    class Client:
        def __init__(self,*a,**kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        async def call_tool(self,*a): return {'results':[]}
    def unexpected(**kw): raise AssertionError('No model call expected')
    monkeypatch.setattr(ai_agent,'Client',Client)
    monkeypatch.setattr(ai_agent,'ChatOpenAI',unexpected)
    monkeypatch.setattr(ai_agent,'unpack',lambda x:x)
    monkeypatch.setattr(ai_agent.cfg,'LLM_KEY','fixture')
    result=asyncio.run(ai_agent.answer('Nothing',None,[],web_search=True))
    assert 'No usable web sources' in result['answer']
