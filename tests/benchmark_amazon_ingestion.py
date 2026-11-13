r"""Opt-in live Kaggle + real MCP/FAISS check in disposable storage (Windows).

Run from demo: ..\..\.venv\Scripts\python.exe -u tests/benchmark_amazon_ingestion.py
Uses the locally configured Kaggle token. No LLM or Tavily credits are used.
"""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings as cfg


class MemoryCounters(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
            'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]


def main():
    import httpx
    assert os.name == 'nt', 'This memory benchmark uses Windows process counters.'
    assert cfg.MAX_CHUNKS == 15_000, 'Configure DEMO_MAX_CHUNKS=15000 before running.'
    base = cfg.DATA_ROOT
    base.mkdir(parents=True, exist_ok=True)
    report = {'chunk_limit': cfg.MAX_CHUNKS,
              'source': 'https://www.kaggle.com/datasets/karkavelrajaj/amazon-sales-dataset',
              'discovery': 'Fixed server-owned candidate; live Kaggle download, real MCP and local embeddings.'}
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    handles, child_handles = [], []
    stop = threading.Event()
    peak = [0]
    health_samples = []
    phase = ['startup', time.monotonic()]

    def monitor():
        next_message = 0
        while not stop.wait(.5):
            total = 0
            for handle in list(handles):
                counters = MemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    total += counters.WorkingSetSize
            peak[0] = max(peak[0], total)
            if time.monotonic() >= next_message:
                print(f'phase={phase[0]} seconds={time.monotonic()-phase[1]:.1f} process_tree_rss_mib={total/2**20:.1f}', flush=True)
                next_message = time.monotonic() + 15

    with tempfile.TemporaryDirectory(prefix='chunk-budget-', dir=base) as temp:
        root = Path(temp)
        cfg.DATA_ROOT, cfg.STAGING, cfg.UPLOADS = root, root/'temp_downloads', root/'uploads'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cfg.AGGREGATOR_URL = f'http://127.0.0.1:{port}/mcp'
        env = {**os.environ, 'DEMO_DATA_ROOT': str(root), 'DEMO_AGGREGATOR_PORT': str(port),
               'DEMO_MAX_CHUNKS': '15000', 'DEMO_OFFLINE_MODE': '1',
               'PYTHONUNBUFFERED': '1', 'TOKENIZERS_PARALLELISM': 'false'}
        log_path = base/'chunk_extension_benchmark.log'
        with log_path.open('w', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, '-m', 'aggregator.aggregator_server'],
                                       cwd=cfg.ROOT, env=env, stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 90
                while True:
                    assert process.poll() is None, f'Aggregator stopped; see {log_path.name}.'
                    try:
                        response = httpx.get(cfg.AGGREGATOR_URL, timeout=2)
                        if response.status_code in {400, 405, 406}: break
                    except httpx.HTTPError: pass
                    assert time.monotonic() < deadline, 'Aggregator startup timed out.'
                    time.sleep(.3)
                # Include Windows virtual-environment launcher grandchildren,
                # not only the direct STDIO wrapper processes.
                tree_query = (
                    '$snapshot=Get-CimInstance Win32_Process; '
                    '$owned=[System.Collections.Generic.List[int]]::new(); '
                    f'$owned.Add({process.pid}); '
                    'for($i=0;$i -lt $owned.Count;$i++){ '
                    'foreach($entry in $snapshot){ '
                    'if($entry.ParentProcessId -eq $owned[$i] -and -not $owned.Contains([int]$entry.ProcessId)){ '
                    '$owned.Add([int]$entry.ProcessId) } } }; '
                    '$owned.ToArray() | ConvertTo-Json -Compress')
                children = subprocess.run(['powershell', '-NoProfile', '-Command', tree_query],
                    capture_output=True, text=True, check=True)
                owned_pids = json.loads(children.stdout)
                if isinstance(owned_pids, int): owned_pids = [owned_pids]
                assert len(owned_pids) >= 3, 'Expected aggregator, RAG and Pandas processes.'
                for pid in [os.getpid(), *owned_pids]:
                    handle = kernel.OpenProcess(0x410 | (1 if pid != os.getpid() else 0), False, pid)
                    assert handle, 'Cannot read owned process memory counters.'
                    handles.append(handle)
                    if pid != os.getpid(): child_handles.append(handle)
                monitor_thread = threading.Thread(target=monitor, daemon=True)
                monitor_thread.start()
                from app import create_app
                from web_search_mcp import dataset_search, search_helpers
                app = create_app()
                app.testing = True
                client = app.test_client()
                bridge = app.extensions['mcp_bridge']
                phase[:] = ['baseline_upload', time.monotonic()]
                result = client.post('/upload', data={'file': (io.BytesIO(b'name,value\nold,1\n'), 'baseline.csv')})
                assert result.status_code == 200, result.get_json()
                baseline = result.get_json()['dataset']['dataset_id']

                async def fixed_search(*args):
                    candidates = search_helpers.prepare_candidates([{'name': 'Amazon Sales Dataset', 'url': report['source']}])['candidates']
                    return {'candidates': candidates, 'total_search_hits': 1, 'warnings': []}
                dataset_search.search = fixed_search
                found = client.post('/dataset-search', json={'topic': 'Amazon Sales', 'limit': 5}).get_json()
                choice = {'search_id': found['search_id'], 'candidate_id': found['candidates'][0]['id']}

                def inspect_file():
                    inspected = client.post('/dataset-search/inspect', json=choice)
                    assert inspected.status_code == 200, inspected.get_json()
                    data = inspected.get_json()
                    item = next(x for x in data['files'] if x['name'] == 'amazon.csv')
                    return {**choice, 'inspection_id': data['inspection_id'], 'file_id': item['id'], 'header': 'present'}

                phase[:] = ['kaggle_export', time.monotonic()]
                selected = inspect_file()
                exported = client.post('/dataset-search/download', json=selected)
                assert exported.status_code == 200 and len(exported.data) == 4_744_481
                assert client.get('/datasets').get_json()['selected_dataset_id'] == baseline
                assert not list(cfg.STAGING.iterdir())
                selected = inspect_file()

                def poll_health():
                    with app.test_client() as checker:
                        while not stop.wait(5):
                            start = time.monotonic()
                            try:
                                result = checker.get('/health')
                                healthy = result.status_code == 200 and all(result.get_json()['local_servers'].values())
                            except Exception:
                                healthy = False
                            health_samples.append({'seconds': round(time.monotonic()-start, 3), 'healthy': healthy})
                health_thread = threading.Thread(target=poll_health, daemon=True)
                health_thread.start()
                phase[:] = ['amazon_ingestion', time.monotonic()]
                imported = client.post('/dataset-search/use', json=selected)
                elapsed = time.monotonic() - phase[1]
                assert imported.status_code == 200, imported.get_json()
                info = imported.get_json()['dataset']
                assert info['rows'] == 1465 and len(info['columns']) == 16
                assert info['chunks'] == 13017 and info['status'] == 'ready'
                assert elapsed < cfg.INGEST_TIMEOUT_SECONDS, 'Import exceeds configured MCP deadline.'
                phase[:] = ['post_import_checks', time.monotonic()]
                assert client.get('/datasets').get_json()['selected_dataset_id'] == info['dataset_id']
                assert not list(cfg.STAGING.iterdir())
                count = bridge.call('aggregate_data', {'dataset_id': info['dataset_id'], 'operation': 'row_count'})
                assert count['value'] == 1465
                passages = bridge.call('rag_search_dataset', {'dataset_id': info['dataset_id'], 'query': 'USB cable', 'top_k': 3})
                assert passages['passages'] and all(p['dataset_id'] == info['dataset_id'] for p in passages['passages'])
                failed = client.post('/upload', data={'file': (io.BytesIO(b'a,b\n1\n'), 'bad.csv')})
                assert failed.status_code == 400
                assert client.get('/datasets').get_json()['selected_dataset_id'] == info['dataset_id']
                assert client.get('/health').get_json()['local_servers'] == {'rag': True, 'pandas': True}
                assert all(s['healthy'] and s['seconds'] < 15 for s in health_samples), health_samples
                report.update(status='passed', rows=info['rows'], columns=len(info['columns']), chunks=info['chunks'],
                              ingest_timeout_seconds=cfg.INGEST_TIMEOUT_SECONDS,
                              import_seconds=round(elapsed, 2), model_state='warm from baseline upload',
                              source_bytes=len(exported.data), health_samples=health_samples,
                              peak_process_tree_working_set_mib=round(peak[0]/2**20, 1),
                              memory_scope='Windows working-set sum: test client, aggregator, RAG and Pandas; sampled every 0.5s.')
            finally:
                stop.set()
                if process.poll() is None:
                    process.terminate()
                try: process.wait(timeout=8)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
                for handle in child_handles:
                    if kernel.WaitForSingleObject(handle, 2000) == 258:
                        kernel.TerminateProcess(handle, 0)
                        kernel.WaitForSingleObject(handle, 2000)
                if 'monitor_thread' in locals(): monitor_thread.join(timeout=2)
                for handle in handles: kernel.CloseHandle(handle)
    destination = base/'chunk_extension_benchmark.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
