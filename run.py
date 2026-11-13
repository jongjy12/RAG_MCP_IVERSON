"""Run the local demo and its aggregator; the aggregator owns the STDIO servers."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parent
# This workspace already has a Python environment. Reuse it if plain Python
# lacks the dependencies; no global installs or environment edits are needed.
if importlib.util.find_spec('fastmcp') is None:
    existing=ROOT.parents[1]/'.venv'/'Scripts'/'python.exe'
    if existing.is_file() and Path(sys.executable).resolve()!=existing.resolve():
        os.execv(str(existing),[str(existing),str(Path(__file__).resolve()),*sys.argv[1:]])
    raise SystemExit('Install the app packages from its requirements.txt in your virtual environment, then run python run.py.')

from config import settings as cfg


def port_open(port):
    with socket.socket() as connection:
        connection.settimeout(.3)
        return connection.connect_ex(('127.0.0.1',port))==0


def wait_for_web(children, port, timeout=90):
    """Announce readiness only after Flask actually serves the page."""
    deadline=time.monotonic()+timeout
    while True:
        for label,child in children:
            code=child.poll()
            if code is not None:
                raise RuntimeError(f'{label} stopped during startup (exit code {code}). Check the startup errors above.')
        try:
            with urlopen(f'http://127.0.0.1:{port}/',timeout=1) as response:
                if response.status==200: return
        except (OSError,URLError):
            pass
        if time.monotonic()>=deadline:
            raise RuntimeError(f'Web interface did not become ready on port {port} within {timeout} seconds. Check the startup errors above.')
        time.sleep(.3)


def main():
    for port in [cfg.AGGREGATOR_PORT,cfg.FLASK_PORT]:
        if port_open(port): raise SystemExit(f'Port {port} is already in use. Stop that process or choose another application port.')
    children=[]
    env={**os.environ,'PYTHONUNBUFFERED':'1','TOKENIZERS_PARALLELISM':'false'}
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    try:
        print('Starting Dataset Analyst… waiting for local services and the web page.',flush=True)
        aggregator=subprocess.Popen([sys.executable,'-m','aggregator.aggregator_server'],cwd=ROOT,env=env,creationflags=flags)
        children.append(aggregator)
        deadline=time.time()+90
        while not port_open(cfg.AGGREGATOR_PORT):
            if aggregator.poll() is not None: raise RuntimeError('Aggregator stopped. See its error above.')
            if time.time()>deadline: raise RuntimeError('Aggregator startup timed out.')
            time.sleep(.3)
        flask=subprocess.Popen([sys.executable,'app.py'],cwd=ROOT,env=env,creationflags=flags)
        children.append(flask)
        wait_for_web([('Aggregator',aggregator),('Flask',flask)],cfg.FLASK_PORT)
        print(f'\nDataset Analyst: http://127.0.0.1:{cfg.FLASK_PORT}\nPress Ctrl+C to stop the demo.\n',flush=True)
        while all(child.poll() is None for child in children): time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None: child.terminate()
        for child in reversed(children):
            try: child.wait(timeout=8)
            except subprocess.TimeoutExpired: child.kill()


if __name__=='__main__': main()

