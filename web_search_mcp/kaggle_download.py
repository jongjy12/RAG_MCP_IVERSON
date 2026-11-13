"""Authenticated Kaggle downloads; credentials never leave the provider worker."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from config import settings as cfg
from web_search_mcp import inspection

DOWNLOAD_TIMEOUT = 180
WORKER = Path(__file__).with_name('kaggle_worker.py')
ERRORS = {
    'auth': 'Kaggle authentication failed. Check KAGGLE_API_TOKEN in FinalProject/.env and restart the app.',
    'forbidden': 'Kaggle denied download permission. Open the dataset on Kaggle, sign in, and check any required consent or access permissions.',
    'not_found': 'The Kaggle dataset was not found or is not visible to this account.',
    'rate_limit': 'Kaggle is limiting downloads. Wait a while before trying again.',
    'too_large': 'The Kaggle archive exceeds the maximum allowed download size.',
    'html': 'Kaggle returned a sign-in or web page instead of a ZIP. Check account access on the source website.',
    'dependency': 'The Kaggle client is unavailable. Install the required app packages in the Python environment and restart.',
    'download': 'The Kaggle download failed. Check your connection and dataset access, then try again.',
}

def dataset_reference(url):
    """Recognize exact Kaggle hosts and supported dataset pages, never arbitrary URLs."""
    if not isinstance(url,str): return None
    try:
        parsed=urlsplit(url)
        if parsed.hostname not in {'www.kaggle.com','kaggle.com'}: return None
        if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in {None,443}:
            raise ValueError('Use a valid HTTPS Kaggle dataset page.')
    except (TypeError,ValueError) as exc:
        raise ValueError('Use a valid HTTPS Kaggle dataset page.') from exc
    match=re.fullmatch(r'/datasets/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)(?:/data|/versions/([1-9][0-9]*))?/?',parsed.path)
    if not match: return None
    return '/'.join(value for value in match.groups() if value)

async def run_worker(reference,folder,token):
    env=os.environ.copy()
    env['KAGGLE_API_TOKEN']=token
    process=await asyncio.create_subprocess_exec(
        sys.executable,'-I',str(WORKER),reference,str(folder),str(cfg.MAX_BYTES),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT):
            output,_=await process.communicate()
        try:
            result=json.loads(output) if len(output)<=2048 else {}
        except (ValueError,UnicodeDecodeError): result={}
        if not isinstance(result,dict): result={}
        if process.returncode or result.get('status')!='ok':
            raise ValueError(ERRORS.get(result.get('error'),'The Kaggle download could not complete. Check the required app packages and dataset access.'))
    except TimeoutError as exc:
        raise ValueError('The Kaggle download timed out. Try a smaller dataset or check your connection.') from exc
    finally:
        # Kill and wait before deleting files, including coroutine cancellation.
        if process.returncode is None:
            process.kill()
            await process.wait()

async def inspect_dataset(page_url):
    reference=dataset_reference(page_url)
    if not reference: raise ValueError('Choose a supported Kaggle dataset page.')
    token=os.getenv('KAGGLE_API_TOKEN','').strip()
    if not token: raise ValueError('Kaggle downloads require KAGGLE_API_TOKEN in FinalProject/.env. Add your token and restart the app.')
    cfg.STAGING.mkdir(parents=True,exist_ok=True)
    # Reuse the inspection prefix so crash leftovers are included in the expiry sweep.
    with tempfile.TemporaryDirectory(prefix='inspect-',dir=cfg.STAGING) as folder:
        await run_worker(reference,folder,token)
        archive=Path(folder)/(reference.split('/')[1]+'.zip')
        if not archive.is_file() or archive.is_symlink(): raise ValueError('Kaggle did not return a ZIP archive.')
        with archive.open('rb') as stream: content=stream.read(cfg.MAX_BYTES+1)
        parts=reference.split('/')
        source_url=f'https://www.kaggle.com/datasets/{parts[0]}/{parts[1]}'
        if len(parts)==3: source_url+=f'/versions/{parts[2]}'
        active=inspection.inspect_download(content,'ZIP',source_url)
        active.update(provider='kaggle',dataset_ref=reference)
        return active
