"""Bounded inspection of selected public dataset pages and archives."""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import stat
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin
from uuid import uuid4

import httpx
from config import settings as cfg
from rag_mcp.ingestion import validate_csv, validate_workbook
from web_search_mcp import search_helpers as helper

MAX_ARCHIVE_ENTRIES = 100
INSPECTION_TTL = 1800

async def fetch(url, *, page=False):
    current=url
    limit=min(cfg.MAX_BYTES, 2*1024*1024) if page else cfg.MAX_BYTES
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, headers={'User-Agent':'DatasetAnalyst/1.0'}) as client:
        for _ in range(6):
            helper._is_public_url(current)
            try:
                async with client.stream('GET',current) as response:
                    if response.status_code in {301,302,303,307,308}:
                        location=response.headers.get('location')
                        if not location: raise ValueError('The source returned an invalid redirect.')
                        current=urljoin(current,location)
                        continue
                    response.raise_for_status()
                    kind=response.headers.get('content-type','').lower()
                    if page and 'html' not in kind: raise ValueError('The source did not return a dataset page.')
                    if not page and 'text/html' in kind: raise ValueError('The download returned a web page rather than a file.')
                    content=bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content)>limit: raise ValueError('The source exceeds the maximum allowed download size.')
                    return bytes(content),current
            except httpx.HTTPError as exc:
                raise ValueError('The dataset source could not be downloaded.') from exc
    raise ValueError('The source exceeded the redirect limit.')

class Links(HTMLParser):
    def __init__(self): super().__init__(); self.hrefs=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower()=='a':
            value=dict(attrs).get('href')
            if value: self.hrefs.append(value)

async def discover(page_url):
    content,final_url=await fetch(page_url,page=True)
    parser=Links(); parser.feed(content.decode('utf-8',errors='replace'))
    links=[]; seen=set()
    for href in parser.hrefs:
        url=helper.direct_file_url(urljoin(final_url,href))
        if url and url not in seen:
            seen.add(url)
            links.append({'id':uuid4().hex,'url':url,'name':helper._candidate_file_name(url),'format':helper.file_format(url)})
            if len(links)>=20: break
    if not links: raise ValueError('No accessible CSV, XLSX, DATA, or ZIP download was found. This source may require you to sign in or have permission to download the file.')
    return links

def cleanup(inspection):
    if not inspection: return
    folder=Path(inspection['folder'])
    root=cfg.STAGING.resolve()
    if folder.name.startswith('inspect-') and not folder.is_symlink() and folder.resolve().parent==root and folder.exists():
        shutil.rmtree(folder)

def cleanup_expired():
    import time
    cfg.STAGING.mkdir(parents=True,exist_ok=True)
    for folder in cfg.STAGING.glob('inspect-*'):
        try:
            if not folder.is_symlink() and folder.is_dir() and time.time()-folder.stat().st_mtime>INSPECTION_TTL:
                cleanup({'folder':str(folder)})
        except OSError: pass

def text_rows(content):
    try: text=content.decode('utf-8-sig')
    except UnicodeDecodeError: text=content.decode('cp1252')
    if '\x00' in text or re.search(r'<(?:!doctype\s+html|html|head|body)\b',text[:2048],re.I):
        raise ValueError('The selected file is not tabular text.')
    try: rows=list(csv.reader(io.StringIO(text,newline=''),strict=True))
    except csv.Error as exc: raise ValueError('The selected file is not valid CSV-like text.') from exc
    while rows and not any(cell.strip() for cell in rows[-1]): rows.pop()
    if not rows or not rows[0] or not any(cell.strip() for cell in rows[0]): raise ValueError('The selected file is empty.')
    width=len(rows[0])
    if len(rows)>cfg.MAX_ROWS+1 or width*len(rows)>cfg.MAX_CELLS: raise ValueError('The selected file exceeds the maximum allowed row or cell count.')
    if any(len(row)!=width for row in rows): raise ValueError('The selected file has inconsistent column counts.')
    return rows

def normalized(content, name, header):
    ext=Path(name).suffix.lower()
    if ext=='.xlsx':
        validate_workbook(content)
        return content,name,[]
    if ext not in {'.csv','.data'}: raise ValueError('Unsupported data file.')
    if header not in {'present','absent'}: raise ValueError('Choose whether the first row contains column names.')
    rows=text_rows(content)
    if header=='absent': rows.insert(0,[f'column_{i+1}' for i in range(len(rows[0]))])
    out=io.StringIO(newline=''); writer=csv.writer(out,lineterminator='\n'); writer.writerows(rows)
    data,warnings=validate_csv(out.getvalue().encode('utf-8'))
    if header=='absent': warnings.append('Generated column names; the first source row was kept as data.')
    return data,Path(name).stem+'.csv',warnings


def detect_header(rows):
    """Conservative, bounded type comparison; text-only tables need a user choice."""
    def kind(value):
        value=value.strip()
        if value.casefold() in {'', 'n/a', 'na', 'null', 'none', 'nan', '-', '?'}: return 'empty'
        try:
            float(value)
            return 'number'
        except ValueError:
            return 'text'

    uncertain={'header':None,'header_detection':'uncertain',
               'header_message':'Check the preview and choose whether the first row contains column names.'}
    if len(rows)<3: return uncertain
    first=rows[0]; following=rows[1:21]
    first_kinds=[kind(value) for value in first]
    if 'empty' in first_kinds: return uncertain
    numeric_columns=[i for i in range(len(first))
                     if sum(kind(row[i])=='number' for row in following)>=max(2,len(following)*.8)]
    if not numeric_columns: return uncertain
    # A record resembling later records is strong evidence against a header.
    if all(kind(first[i])=='number' for i in numeric_columns):
        # Whole-number column labels (especially years) can resemble data.
        # Ask for a choice when the numbers do not distinguish these cases.
        numeric_first=[first[i].strip() for i in numeric_columns]
        if all(float(value).is_integer() for value in numeric_first):
            return uncertain
        return {'header':'absent','header_detection':'likely_absent',
                'header_message':'The first row appears to be data. All rows will be kept with generated column names. Change below if needed.'}
    names=[cell.strip() for cell in first]
    if (all(names) and len(set(names))==len(names)
            and all(value=='text' for value in first_kinds)
            and all(re.fullmatch(r'[\w][\w .()/%-]{0,99}',cell) for cell in names)):
        return {'header':'present','header_detection':'likely_present',
                'header_message':'The first row appears to contain column names. Change below if needed.'}
    return uncertain

def _safe_member(info):
    name=info.orig_filename
    parts=PurePosixPath(name).parts
    mode=info.external_attr >> 16
    return ('\x00' not in name and not name.startswith(('/', '\\')) and '\\' not in name and not re.match(r'^[A-Za-z]:',name)
            and '..' not in parts and not stat.S_ISLNK(mode) and not (info.flag_bits & 1))

async def inspect(url):
    data,final_url=await fetch(url)
    return inspect_download(data,helper.file_format(url) or helper.file_format(final_url),url)

def inspect_download(data,kind,url):
    """Inspect bounded bytes from either a public URL or an authenticated provider."""
    if len(data)>cfg.MAX_BYTES: raise ValueError('The source exceeds the maximum allowed download size.')
    cfg.STAGING.mkdir(parents=True,exist_ok=True)
    folder=Path(tempfile.mkdtemp(prefix='inspect-',dir=cfg.STAGING))
    result={'id':uuid4().hex,'folder':str(folder),'files':{},'url':url,'source_size_bytes':len(data),'source_format':kind}
    try:
        if kind=='ZIP':
            (folder/'archive.zip').write_bytes(data)
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    entries=archive.infolist()
                    if len(entries)>MAX_ARCHIVE_ENTRIES or sum(x.file_size for x in entries)>cfg.MAX_EXPANDED_BYTES:
                        raise ValueError('The archive exceeds the maximum allowed extraction size or file count.')
                    if any(not _safe_member(x) for x in entries): raise ValueError('The archive contains an unsafe or encrypted entry.')
                    if any(Path(x.filename).suffix.lower()=='.zip' for x in entries): raise ValueError('Nested ZIP archives are not supported.')
                    for entry in entries:
                        if entry.is_dir() or Path(entry.filename).suffix.lower() not in {'.csv','.xlsx','.data'}: continue
                        if entry.file_size>cfg.MAX_BYTES or entry.file_size>max(1,entry.compress_size)*500:
                            raise ValueError('An archive member exceeds the maximum allowed extraction size.')
                        with archive.open(entry) as stream:
                            content=stream.read(cfg.MAX_BYTES+1)
                        if len(content)>cfg.MAX_BYTES: raise ValueError('An archive member exceeds the maximum allowed file size.')
                        _add_file(result,Path(entry.filename).name,content)
            except (zipfile.BadZipFile,RuntimeError,OSError) as exc: raise ValueError('The ZIP archive could not be read.') from exc
        elif kind in {'CSV','XLSX','DATA'}:
            _add_file(result,helper._candidate_file_name(url),data)
        else: raise ValueError('The selected source is not a supported data file or ZIP archive.')
        if not result['files']: raise ValueError('No supported data files were found in the download.')
        return result
    except Exception:
        cleanup(result)
        raise

def _add_file(result,name,content):
    ext=Path(name).suffix.lower()
    row_count=column_count=None
    if ext=='.xlsx':
        if not zipfile.is_zipfile(io.BytesIO(content)): return
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if 'xl/workbook.xml' not in archive.namelist(): return
        preview=[]; header=None; detection={}
    else:
        try: rows=text_rows(content)
        except ValueError: return
        preview=[[cell[:200] for cell in row[:20]] for row in rows[:5]]
        detection=detect_header(rows); header=detection['header']
        row_count=len(rows); column_count=len(rows[0])
    file_id=uuid4().hex
    (Path(result['folder'])/(file_id+'.bin')).write_bytes(content)
    item={'id':file_id,'name':name,'format':ext.lstrip('.').upper(),'preview':preview,'header':header,'size_bytes':len(content),**detection}
    if row_count is not None: item.update(row_count=row_count,column_count=column_count)
    result['files'][file_id]=item

def read_selected(inspection,file_id,header):
    item=inspection['files'].get(file_id)
    if not item: raise ValueError('Choose a file from this inspection.')
    content=(Path(inspection['folder'])/(file_id+'.bin')).read_bytes()
    return normalized(content,item['name'],header),item
