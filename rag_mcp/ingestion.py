from __future__ import annotations

import csv
import io
import re
import shutil
import sys
import threading
import zipfile
from datetime import datetime, timezone
from uuid import uuid4

import numpy as np
import pandas as pd
from openpyxl import Workbook, load_workbook

from config import settings as cfg
from pandas_mcp.analytics import filtered
from rag_mcp import table_extractor as parser
from shared import storage
from shared import progress

_model = None
_indexes = {}
_ingestion_lock = threading.Lock()


def get_model():
    global _model
    if _model is None:
        import os
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(cfg.EMBEDDING_MODEL, local_files_only=os.getenv('DEMO_OFFLINE_MODE') == '1')
    return _model


def invalidate_old_indexes():
    for info in storage.catalog():
        if info.get('status') == 'ready':
            info['status'] = 'needs_reingestion'
            storage.atomic_json(storage.dataset_dir(info['dataset_id'])/'dataset.json', info)


def validate_csv(content: bytes):
    warnings = []
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = content.decode('cp1252')
        warnings.append('Source decoded as Windows-1252; export uses UTF-8.')
    if '\x00' in text or re.search(r'<(?:!doctype\s+html|html|head|body)\b', text[:2048], re.I):
        raise ValueError('The source returned HTML or binary content, not a CSV dataset.')
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        header = next(reader, None)
        if not header or not any(c.strip() for c in header):
            raise ValueError('The CSV must have a header row.')
        unique, seen = [], set()
        for index, item in enumerate(header):
            base = item.strip() or f'Column {index+1}'
            candidate, serial = base, 2
            while candidate in seen:
                candidate = f'{base}_{serial}'
                serial += 1
            unique.append(candidate)
            seen.add(candidate)
        if unique != header:
            warnings.append('Blank/duplicate headers normalized to unique names: ' + ', '.join(unique))
        rows = []
        for line, row in enumerate(reader, 2):
            if len(row) != len(header):
                raise ValueError(f'CSV record {line} has {len(row)} fields; expected {len(header)}. No rows were skipped.')
            rows.append(row)
            if len(rows)>cfg.MAX_ROWS or len(rows)*len(header)>cfg.MAX_CELLS:
                raise ValueError('The CSV exceeds the maximum allowed row or cell count.')
        if not rows:
            raise ValueError('The CSV has no data rows.')
    except csv.Error as exc:
        raise ValueError(f'Malformed CSV: {exc}') from exc
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(unique)
    writer.writerows(rows)
    exported = output.getvalue().encode('utf-8')
    if len(exported)>cfg.MAX_BYTES:
        raise ValueError('The processed CSV exceeds the maximum allowed file size.')
    return exported, warnings


def validate_workbook(content: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > cfg.MAX_EXPANDED_BYTES:
                raise ValueError('Expanded workbook exceeds 100 MiB.')
            if any(item.flag_bits & 1 for item in archive.infolist()):
                raise ValueError('Encrypted workbooks are unsupported.')
    except zipfile.BadZipFile as exc:
        raise ValueError('The file is not a valid XLSX workbook.') from exc
    workbook = load_workbook(io.BytesIO(content), read_only=False, data_only=True)
    # Inspect populated cells, not styled empty Excel tails.
    total = 0
    for sheet in workbook:
        populated = [c for c in sheet._cells.values() if c.value is not None]
        total += len(populated)
        if total>cfg.MAX_CELLS or any(c.row>cfg.MAX_ROWS+1 or c.column>1000 for c in populated):
            raise ValueError('Workbook exceeds the maximum allowed row or cell count.')
    workbook.close()


def convert_xls(content: bytes):
    import xlrd
    try:
        book = xlrd.open_workbook(file_contents=content, formatting_info=True, logfile=sys.stderr)
    except Exception as exc:
        raise ValueError('The file is not a readable legacy XLS workbook.') from exc
    workbook = Workbook()
    workbook.remove(workbook.active)
    cells = 0
    for source in book.sheets():
        cells += source.nrows*source.ncols
        if cells>cfg.MAX_CELLS or source.nrows>cfg.MAX_ROWS+1:
            raise ValueError('Legacy workbook exceeds the maximum allowed row or cell count.')
        sheet = workbook.create_sheet(source.name)
        for r in range(source.nrows):
            for c in range(source.ncols):
                value = source.cell_value(r,c)
                kind = source.cell_type(r,c)
                if kind == xlrd.XL_CELL_DATE:
                    value = xlrd.xldate.xldate_as_datetime(value, book.datemode)
                elif kind == xlrd.XL_CELL_BOOLEAN:
                    value = bool(value)
                elif kind in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
                    continue
                cell = sheet.cell(r+1,c+1,value)
                if kind == xlrd.XL_CELL_DATE:
                    cell.number_format = 'yyyy-mm-dd'
                else:
                    xf = book.xf_list[source.cell_xf_index(r,c)]
                    fmt = book.format_map.get(xf.format_key)
                    if fmt: cell.number_format = fmt.format_str
        for rlo,rhi,clo,chi in source.merged_cells:
            sheet.merge_cells(start_row=rlo+1,end_row=rhi,start_column=clo+1,end_column=chi)
    result = io.BytesIO()
    workbook.save(result)
    book.release_resources()
    return result.getvalue()


def normalize_tables(tables, workbook_bytes=None):
    workbook = load_workbook(io.BytesIO(workbook_bytes), data_only=True) if workbook_bytes else None
    try:
        for table in tables:
            frame = table['frame'].copy()
            unique, seen = [], set()
            for index, column in enumerate(frame.columns):
                base = str(column).strip() or f'Column {index+1}'
                candidate, serial = base, 2
                while candidate in seen:
                    candidate = f'{base}_{serial}'
                    serial += 1
                unique.append(candidate)
                seen.add(candidate)
            frame.columns = unique
            table['percentage_columns'] = []
            for index,column in enumerate(frame.columns):
                values = frame[column].map(lambda v: None if isinstance(v,str) and not v.strip() else v)
                explicit = values.map(lambda v: isinstance(v,str) and bool(re.fullmatch(r'\s*[+-]?\d+(?:\.\d+)?\s*%\s*',v)))
                if explicit.any():
                    values = values.map(lambda v: float(v.strip().rstrip('%')) if isinstance(v,str) and re.fullmatch(r'\s*[+-]?\d+(?:\.\d+)?\s*%\s*',v) else v)
                    # Mixed plain numerics have unknown scale, so do not certify the column.
                    nonblank = values.notna()
                    if int(explicit.sum()) == int(nonblank.sum()): table['percentage_columns'].append(column)
                if workbook and table.get('bounds') and table.get('sheet'):
                    _,_,min_col,_ = table['bounds']
                    sheet = workbook[table['sheet']]
                    populated = [sheet.cell(row,min_col+index) for row in table['source_rows']]
                    numeric = [c for c in populated if isinstance(c.value,(int,float)) and not isinstance(c.value,bool)]
                    if numeric and all('%' in c.number_format for c in numeric):
                        table['percentage_columns'].append(column)
                frame[column] = values
            table['frame'] = frame
    finally:
        if workbook: workbook.close()
    return tables


def rag_load_dataset(source_ref: str, dataset_name: str | None = None):
    if not _ingestion_lock.acquire(blocking=False):
        raise ValueError('Another dataset is being prepared. Wait for it to finish.')
    folder = None
    dataset_id = uuid4().hex
    try:
        if len(_indexes)>=cfg.MAX_DATASETS:
            raise ValueError(f'You can have up to {cfg.MAX_DATASETS} prepared datasets at a time. Remove a dataset or restart the application before adding another.')
        path, staged = storage.resolve_source(source_ref)
        operation_id = staged.get('operation_id')
        progress.update(operation_id, 'Reading and validating data')
        if path.stat().st_size>cfg.MAX_BYTES:
            raise ValueError('The source exceeds the maximum allowed file size.')
        content = path.read_bytes()
        warnings = []
        workbook_bytes = None
        if path.suffix.lower() == '.csv':
            content, warnings = validate_csv(content)
            tables = parser.parse_csv_table(content)
        else:
            workbook_bytes = convert_xls(content) if path.suffix.lower()=='.xls' else content
            validate_workbook(workbook_bytes)
            if _model is None: progress.update(operation_id, 'Loading embedding model')
            model = get_model()
            progress.update(operation_id, 'Detecting workbook tables')
            tables, warnings = parser.parse_excel_tables(workbook_bytes, model)
            if path.suffix.lower()=='.xls': warnings.append('Legacy XLS converted to values/date/percentage-aware XLSX for table detection. Original XLS retained.')
        tables = normalize_tables(tables, workbook_bytes)
        warnings = list(staged.get('source', {}).get('warnings', [])) + warnings
        if not tables or not any(len(t['frame']) for t in tables):
            raise ValueError('No data rows found.')
        if sum(t['frame'].size for t in tables)>cfg.MAX_CELLS:
            raise ValueError('Extracted data exceeds the maximum allowed cell count.')
        if _model is None: progress.update(operation_id, 'Loading embedding model')
        model = get_model()
        progress.update(operation_id, 'Preparing searchable text')
        # Never attach one workbook-wide inferred project name to unrelated rows.
        chunks = parser.build_chunks(tables, getattr(model,'tokenizer',None))
        if not chunks or len(chunks)>cfg.MAX_CHUNKS:
            raise ValueError(f'Dataset must contain 1-{cfg.MAX_CHUNKS} searchable chunks. No sampling was performed.')
        import faiss
        progress.update(operation_id, f'Creating embeddings for {len(chunks):,} text chunks')
        vectors = np.ascontiguousarray(model.encode([c['text'] for c in chunks], batch_size=64,
                                                   show_progress_bar=True, convert_to_numpy=True,
                                                   normalize_embeddings=True),dtype=np.float32)
        progress.update(operation_id, 'Building search index and saving data')
        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)
        folder = storage.dataset_dir(dataset_id)
        folder.mkdir(parents=True)
        # Preserve uploaded bytes, including the original legacy workbook.
        (folder/path.name).write_bytes(path.read_bytes())
        summaries = [storage.save_table(folder,t) for t in tables]
        info = {'dataset_id':dataset_id,'name':(dataset_name or staged['name'])[:200], 'filename':path.name,
                'format':path.suffix.lower().lstrip('.'), 'status':'ready',
                'rows':sum(len(t['frame']) for t in tables if t['role']=='records' and t.get('searchable',True)),
                'columns':list(dict.fromkeys(str(c) for t in tables for c in t['frame'].columns)),
                'tables':summaries,'warnings':warnings,'source':staged.get('source',{}),
                'chunks':len(chunks),'created_at':datetime.now(timezone.utc).isoformat()}
        for chunk in chunks:
            chunk['dataset_id'] = dataset_id
            chunk['filename'] = path.name
        _indexes[dataset_id] = (index,chunks)
        storage.atomic_json(folder/'dataset.json',info)
        return info
    except Exception:
        _indexes.pop(dataset_id,None)
        if folder and folder.exists(): shutil.rmtree(folder)
        raise
    finally:
        _ingestion_lock.release()


def rag_get_dataset_info(dataset_id: str):
    if dataset_id not in _indexes:
        raise ValueError('FAISS index is unavailable. Re-ingest the dataset after restarting RAG.')
    return storage.manifest(dataset_id)


def rag_search_dataset(dataset_id: str, query: str, top_k: int = 5,
                       table_id: str | None = None, filters: list[dict] | None = None):
    rag_get_dataset_info(dataset_id)
    if not isinstance(query,str) or not query.strip() or len(query)>2000:
        raise ValueError('Supply a query of 1-2000 characters.')
    if not 1<=top_k<=10: raise ValueError('top_k must be 1 through 10.')
    index,chunks = _indexes[dataset_id]
    frame,table = storage.load_table(dataset_id,table_id)
    selected = filtered(frame,filters)
    rows = {table['source_rows'][i] for i in selected.index}
    candidates = [i for i,c in enumerate(chunks) if c['table_id']==table['id'] and c['source_row'] in rows]
    if not candidates:
        return {'dataset_id':dataset_id,'table_id':table['id'],'filters':filters or [],'passages':[]}
    # Scope is applied before ranking: reconstruct just eligible vectors from FAISS.
    import faiss
    scoped = faiss.IndexFlatIP(index.d)
    scoped.add(np.array([index.reconstruct(i) for i in candidates],dtype=np.float32))
    query_vector = np.ascontiguousarray(get_model().encode([query],convert_to_numpy=True,normalize_embeddings=True),dtype=np.float32)
    scores,positions = scoped.search(query_vector,min(top_k,len(candidates)))
    passages = [{**chunks[candidates[int(p)]], 'score':float(score)} for p,score in zip(positions[0],scores[0]) if p>=0]
    return {'dataset_id':dataset_id,'table_id':table['id'],'filters':filters or [],'passages':passages,
            'scope_note':'Only explicitly selected table/project rows are searched. Resolve another table for context if needed.'}

