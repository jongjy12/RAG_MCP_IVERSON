"""Isolated official Kaggle client with bounded streaming and sanitized status output."""
from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path


class DownloadTooLarge(Exception):
    pass


class WebPageDownload(Exception):
    pass


def save_response(response,outfile,limit):
    """Enforce the cap before writing each chunk, even without Content-Length."""
    with response:
        response.raise_for_status()
        if 'text/html' in response.headers.get('Content-Type','').lower(): raise WebPageDownload()
        size=response.headers.get('Content-Length')
        if size and int(size)>limit: raise DownloadTooLarge()
        received=0
        with Path(outfile).open('wb') as stream:
            for chunk in response.iter_content(chunk_size=65536):
                received+=len(chunk)
                if received>limit: raise DownloadTooLarge()
                stream.write(chunk)


def download(reference,folder,limit):
    from kagglesdk import KaggleClient, KaggleEnv
    from kagglesdk.datasets.types.dataset_api_service import ApiDownloadDatasetRequest

    parts=reference.split('/')
    request=ApiDownloadDatasetRequest()
    request.owner_slug,request.dataset_slug=parts[:2]
    if len(parts)==3: request.dataset_version_number=int(parts[2])
    # Explicit token and production host avoid unrelated credential discovery.
    with KaggleClient(env=KaggleEnv.PROD,api_token=os.environ['KAGGLE_API_TOKEN'],
                      user_agent='DatasetAnalyst/1.0') as client:
        response=client.datasets.dataset_api_client.download_dataset(request)
        save_response(response,Path(folder)/(parts[1]+'.zip'),limit)


def main():
    status={'error':'download'}
    # Upstream diagnostics can contain request URLs/headers. Emit only our error codes.
    with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
        try:
            download(sys.argv[1],sys.argv[2],int(sys.argv[3]))
            status={'status':'ok'}
        except ImportError: status={'error':'dependency'}
        except DownloadTooLarge: status={'error':'too_large'}
        except WebPageDownload: status={'error':'html'}
        except SystemExit: status={'error':'auth'}
        except Exception as exc:
            response=getattr(exc,'response',None)
            code=getattr(response,'status_code',None) or getattr(exc,'status_code',None)
            status={'error':{401:'auth',403:'forbidden',404:'not_found',429:'rate_limit'}.get(code,'download')}
    print(json.dumps(status))
    return 0 if status.get('status')=='ok' else 1


if __name__=='__main__':
    raise SystemExit(main())
