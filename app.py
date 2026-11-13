"""Local Flask UI. Both ingestion paths invoke the same RAG MCP tool."""
from __future__ import annotations

import io
import logging
import secrets
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from uuid import uuid4
from functools import wraps

from flask import Flask, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException

from agent.mcp_client import Bridge, UnknownCompletion
from agent import ai_agent
from config import settings as cfg
from shared import storage
from shared import progress
from web_search_mcp import dataset_search
from web_search_mcp import inspection
from web_search_mcp import kaggle_download


def create_app(bridge=None):
    inspection.cleanup_expired()
    app=Flask(__name__)
    app.secret_key=secrets.token_hex(32)
    app.config.update(MAX_CONTENT_LENGTH=cfg.MAX_BYTES+1024*1024,
                      SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict')
    mcp=bridge or Bridge()
    app.extensions['mcp_bridge']=mcp
    state_lock=threading.RLock()
    mutation_lock=threading.Lock()
    states={}

    def state():
        with state_lock:
            if 'sid' not in session: session['sid']=uuid4().hex
            sid=session['sid']
            now=time.time()
            for key in list(states):
                if now-states[key]['updated']>7200:
                    inspection.cleanup(states[key].get('inspection'))
                    states.pop(key)
            result=states.setdefault(sid,{'selected':None,'search':None,'history':{},'updated':now,'chat_busy':False})
            active=result.get('inspection')
            if active and now-active['created']>inspection.INSPECTION_TTL:
                inspection.cleanup(active)
                result.pop('inspection',None)
            result['updated']=now
            return result

    @app.before_request
    def local_boundary():
        if request.host.split(':')[0] not in {'127.0.0.1','localhost','[::1]'}:
            return jsonify(error='Use the local application address.'),403
        if request.method=='POST' and request.headers.get('Origin'):
            from urllib.parse import urlsplit
            if urlsplit(request.headers['Origin']).netloc != request.host:
                return jsonify(error='Cross-origin requests are not allowed.'),403

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc,HTTPException):
            return jsonify(error='File exceeds the maximum allowed upload size.' if exc.code==413 else exc.description),exc.code
        if isinstance(exc,(FutureTimeout,UnknownCompletion)):
            return jsonify(error='Completion is unknown. Do not retry automatically; check the application status or restart before retrying.',unknown_completion=True),504
        from fastmcp.exceptions import ToolError
        if isinstance(exc,(ValueError,FileNotFoundError,ToolError)):
            return jsonify(error=str(exc)),400
        app.logger.exception('Demo request failed')
        return jsonify(error='The local service or model is unavailable. Check the terminal and configuration.'),503

    def json_body():
        value=request.get_json(silent=True)
        if not isinstance(value,dict): raise ValueError('Send a JSON object.')
        return value

    def selected_candidate(body,current):
        search=current.get('search')
        if not search or time.time()-search['created']>3600 or body.get('search_id')!=search['id']:
            raise ValueError('Search selection expired. Search again.')
        candidate=next((item for item in search['candidates'] if item['id']==body.get('candidate_id')),None)
        if not candidate: raise ValueError('Choose a candidate from this search.')
        return candidate

    def selected_inspection(body,current):
        candidate=selected_candidate(body,current)
        active=current.get('inspection')
        if not active or active['id']!=body.get('inspection_id') or active['candidate_id']!=candidate['id']:
            raise ValueError('File selection expired. Inspect the dataset again.')
        return candidate,active

    def discard_inspection(current):
        inspection.cleanup(current.pop('inspection',None))
        current.pop('download_links',None)

    def operation_id():
        body=request.get_json(silent=True) if request.is_json else request.form
        value=(body or {}).get('operation_id')
        if value: storage.safe_id(value)
        if value and value not in state().get('operations',{}):
            raise ValueError('Preparation operation expired. Start again.')
        return value

    def finish_operation(op, stage, status):
        if op:
            with state_lock:
                state()['operations'][op]['terminal']={'stage':stage,'state':status,'updated':time.time()}
            progress.update(op,stage,status)

    def tracked(stage, final_stage):
        def decorate(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                op=operation_id()
                if op:
                    with state_lock:
                        value=progress.read(op)
                        expected={'upload':'upload','inspect_candidate':'inspect','use_candidate':'use','download_only':'download'}[fn.__name__]
                        if value['kind']!=expected: raise ValueError('Preparation operation belongs to another action.')
                        if value['state']!='running' or state()['operations'][op]['claimed']:
                            raise ValueError('Preparation operation has already been used.')
                        state()['operations'][op]['claimed']=True
                progress.update(op,stage)
                try:
                    result=fn(*args, **kwargs)
                    code=result[1] if isinstance(result,tuple) else 200
                    finish_operation(op,final_stage if code<400 else 'Preparation could not start', 'complete' if code<400 else 'failed')
                    return result
                except (FutureTimeout,UnknownCompletion):
                    finish_operation(op,'Completion unknown. Do not retry automatically.','unknown')
                    raise
                except Exception:
                    finish_operation(op,'Preparation failed. See the error message.','failed')
                    raise
            return wrapped
        return decorate

    @app.post('/preparation/start')
    def start_preparation():
        kind=json_body().get('kind')
        if kind not in {'upload','inspect','use','download'}: raise ValueError('Invalid preparation kind.')
        current=state()
        with state_lock:
            now=time.time(); operations=current.setdefault('operations',{})
            current['operations']=operations={key:value for key,value in operations.items() if now-value['created']<7200}
            if len(operations)>=50: raise ValueError('Too many recent preparations. Restart the application to continue.')
            op=uuid4().hex
            progress.cleanup(); progress.start(op,kind)
            operations[op]={'created':now,'claimed':False}
        return jsonify(operation_id=op)

    @app.get('/preparation/<op>')
    def preparation_status(op):
        record=state().get('operations',{}).get(op)
        if not record: return jsonify(error='Preparation not found.'),404
        snapshot=progress.read(op)
        # A late RAG stage write must not hide a Flask timeout/final outcome.
        snapshot.update(record.get('terminal',{}))
        return jsonify(snapshot)

    @app.get('/datasets/preview')
    def dataset_preview():
        current=state(); dataset_id=request.args.get('dataset_id')
        if not dataset_id or dataset_id!=current['selected']: raise ValueError('Select this dataset before previewing it.')
        info=mcp.call('rag_get_dataset_info',{'dataset_id':dataset_id})
        table_id=request.args.get('table_id')
        if not any(t['table_id']==table_id for t in info['tables']): raise ValueError('Choose a table from this dataset.')
        return jsonify(mcp.call('filter_dataset',{'dataset_id':dataset_id,'table_id':table_id,'limit':10}))

    async def ingest(source_ref):
        completed = False
        try:
            result = await mcp.call_async('rag_load_dataset',{'source_ref':source_ref})
            completed = True
            return result
        except UnknownCompletion:
            raise
        except Exception:
            completed = True
            raise
        finally:
            # This coroutine continues after a Flask wait timeout, so cleanup only
            # occurs when the actual MCP attempt returns, never at browser timeout.
            if completed:
                storage.cleanup_source(source_ref)
                mutation_lock.release()

    @app.get('/')
    def home(): return render_template('index.html')

    @app.get('/health')
    def health():
        with state_lock: inspection.cleanup_expired()
        return jsonify(mcp.call('demo_status',{}))

    @app.get('/datasets')
    def datasets():
        current=state()
        result=[]
        for info in storage.catalog():
            if info.get('status')=='ready':
                try: mcp.call('rag_get_dataset_info',{'dataset_id':info['dataset_id']})
                except Exception:
                    info['status']='needs_reingestion'
            result.append(info)
        ready={x['dataset_id'] for x in result if x['status']=='ready'}
        if current['selected'] not in ready: current['selected']=None
        return jsonify(datasets=result,selected_dataset_id=current['selected'])

    @app.post('/datasets/select')
    def select():
        body=json_body()
        dataset_id=body.get('dataset_id')
        info=mcp.call('rag_get_dataset_info',{'dataset_id':dataset_id})
        state()['selected']=dataset_id
        return jsonify(dataset=info)

    @app.post('/upload')
    @tracked('Receiving and staging file','Dataset ready')
    def upload():
        current=state()
        uploaded=request.files.get('file')
        if uploaded is None or not uploaded.filename: raise ValueError('Choose a CSV, XLSX, or XLS file.')
        if not mutation_lock.acquire(blocking=False): return jsonify(error='Another dataset is being prepared.'),409
        source_ref=None
        try:
            source_ref=storage.stage_file(uploaded.filename,uploaded.stream.read(cfg.MAX_BYTES+1),operation_id=operation_id())
        except Exception:
            mutation_lock.release()
            raise
        info=mcp.run(ingest(source_ref),timeout=cfg.INGEST_TIMEOUT_SECONDS+40)
        current['selected']=info['dataset_id']
        return jsonify(dataset=info)

    @app.post('/dataset-search')
    def search():
        current=state()
        body=json_body()
        result=mcp.run(dataset_search.search(mcp,body.get('topic',''),body.get('limit',5)),timeout=120)
        discard_inspection(current)
        search_id=uuid4().hex
        current['search']={'id':search_id,'created':time.time(),'candidates':result['candidates']}
        return jsonify(search_id=search_id,**result)

    @app.post('/dataset-search/inspect')
    @tracked('Downloading and inspecting dataset files','Files ready to review')
    def inspect_candidate():
        current=state(); body=json_body(); candidate=selected_candidate(body,current)
        choice=body.get('download_id')
        page_url=candidate.get('page_url') or candidate.get('url')
        if kaggle_download.dataset_reference(page_url):
            discard_inspection(current)
            active=mcp.run(kaggle_download.inspect_dataset(page_url),timeout=240)
        else:
            if choice:
                saved=current.get('download_links') or {}
                if saved.get('candidate_id')!=candidate['id']: raise ValueError('Download selection expired. Inspect the dataset again.')
                link=next((x for x in saved.get('links',[]) if x['id']==choice),None)
                if not link: raise ValueError('Choose a download from this dataset page.')
                url=link['url']
            elif candidate.get('download_url'):
                discard_inspection(current)
                url=candidate['download_url']
            else:
                discard_inspection(current)
                links=mcp.run(inspection.discover(page_url),timeout=120)
                if len(links)>1:
                    current['download_links']={'candidate_id':candidate['id'],'links':links}
                    return jsonify(downloads=[{'id':x['id'],'name':x['name'],'format':x['format']} for x in links])
                url=links[0]['url']
            inspection.cleanup(current.pop('inspection',None))
            active=mcp.run(inspection.inspect(url),timeout=320)
        active.update(created=time.time(),candidate_id=candidate['id'])
        current['inspection']=active
        def expire():
            with state_lock:
                if current.get('inspection') is active: discard_inspection(current)
        timer=threading.Timer(inspection.INSPECTION_TTL,expire); timer.daemon=True; timer.start()
        return jsonify(inspection_id=active['id'],source_size_bytes=active.get('source_size_bytes'),
                       source_format=active.get('source_format'),files=list(active['files'].values()))

    @app.post('/dataset-search/cancel-inspection')
    def cancel_inspection():
        current=state(); body=json_body()
        active=current.get('inspection')
        if active and active['id']==body.get('inspection_id'): discard_inspection(current)
        return jsonify(ok=True)

    @app.post('/dataset-search/use')
    @tracked('Validating selected file','Dataset ready')
    def use_candidate():
        current=state()
        body=json_body(); candidate=selected_candidate(body,current)
        if not mutation_lock.acquire(blocking=False): return jsonify(error='Another dataset is being prepared.'),409
        async def prepare():
            try:
                provider_source={}
                if body.get('inspection_id'):
                    _,active=selected_inspection(body,current)
                    with state_lock:
                        try:
                            (content,filename,warnings),item=inspection.read_selected(active,body.get('file_id'),body.get('header'))
                            download_url=active['url']
                            provider_source={key:active[key] for key in ('provider','dataset_ref') if key in active}
                        finally:
                            discard_inspection(current)
                else:
                    content,warnings=await dataset_search.download(candidate)
                    filename=candidate['file_name']; download_url=candidate['download_url']; item={'name':filename}
                source_ref=storage.stage_file(filename,content,
                                              source={'page_url':candidate.get('page_url') or candidate.get('url'),'download_url':download_url,'selected_file':item['name'],'warnings':warnings,**provider_source},
                                              name=candidate['name'],operation_id=body.get('operation_id'))
            except Exception:
                mutation_lock.release()
                raise
            return await ingest(source_ref)
        # The automated path may download a direct CSV before the MCP import.
        info=mcp.run(prepare(),timeout=cfg.INGEST_TIMEOUT_SECONDS+140)
        current['selected']=info['dataset_id']
        return jsonify(dataset=info)

    @app.post('/dataset-search/download')
    @tracked('Preparing file for download','File ready to download')
    def download_only():
        current=state(); body=json_body(); candidate=selected_candidate(body,current)
        if body.get('inspection_id'):
            _,active=selected_inspection(body,current)
            with state_lock:
                try: (content,filename,_),_=inspection.read_selected(active,body.get('file_id'),body.get('header'))
                finally: discard_inspection(current)
        else:
            content,_=mcp.run(dataset_search.download(candidate),timeout=100); filename=candidate['file_name']
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if filename.lower().endswith('.xlsx') else 'text/csv; charset=utf-8'
        return send_file(io.BytesIO(content),mimetype=mime,as_attachment=True,download_name=filename)

    @app.get('/chat/history')
    def history():
        current=state()
        return jsonify(dataset_id=current['selected'],messages=current['history'].get(current['selected'],[]))

    @app.post('/ask')
    def ask():
        current=state()
        body=json_body()
        question=body.get('question','')
        if not isinstance(question,str) or not question.strip() or len(question)>4000: raise ValueError('Enter a question of 1-4000 characters.')
        dataset_id=current['selected']
        if not dataset_id: raise ValueError('Select a ready dataset first.')
        if body.get('dataset_id',dataset_id)!=dataset_id: raise ValueError('Question dataset differs from the selected dataset.')
        with state_lock:
            if current['chat_busy']: return jsonify(error='Wait for the current answer.'),409
            current['chat_busy']=True
            previous=list(current['history'].get(dataset_id,[]))
        try:
            result=mcp.run(ai_agent.answer(question,dataset_id,previous),timeout=300)
            info=storage.manifest(dataset_id)
            result['dataset_name']=info['name']
            records=current['history'].setdefault(dataset_id,[])
            records.append({'question':question,**result})
            del records[:-12]
            return jsonify(result)
        finally:
            current['chat_busy']=False

    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1',port=cfg.FLASK_PORT,debug=False,threaded=True,use_reloader=False)

