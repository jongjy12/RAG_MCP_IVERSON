from __future__ import annotations

import json
import math
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
from werkzeug.utils import secure_filename

from config import settings as cfg


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('Invalid managed dataset/source reference.')
    return value


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(json_safe(value), ensure_ascii=False, allow_nan=False), encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def stage_file(filename: str, content: bytes, source: dict | None = None, name: str | None = None, operation_id: str | None = None) -> str:
    filename = secure_filename(filename)
    if not filename or Path(filename).suffix.lower() not in {'.csv', '.xlsx', '.xls'}:
        raise ValueError('Choose a CSV, XLSX, or XLS file.')
    if not content or len(content) > cfg.MAX_BYTES:
        raise ValueError(f'The file must contain data and be at most {cfg.MAX_BYTES // 1024 // 1024} MiB.')
    source_ref = uuid4().hex
    folder = cfg.STAGING / source_ref
    folder.mkdir(parents=True)
    (folder / filename).write_bytes(content)
    atomic_json(folder / 'source.json', {'filename': filename, 'name': (name or filename)[:200], 'source': source or {}, 'operation_id': operation_id})
    return source_ref


def resolve_source(source_ref: str):
    folder = cfg.STAGING / safe_id(source_ref)
    manifest = read_json(folder / 'source.json')
    path = (folder / manifest['filename']).resolve()
    if not path.is_relative_to(cfg.STAGING.resolve()) or not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError('The staged source is outside the managed folder.')
    if path.suffix.lower() not in {'.csv', '.xls', '.xlsx'}:
        raise ValueError('Unsupported staged source.')
    return path, manifest


def cleanup_source(source_ref: str):
    folder = cfg.STAGING / safe_id(source_ref)
    if folder.is_symlink():
        raise ValueError('Invalid staging directory.')
    if folder.exists() and folder.resolve().is_relative_to(cfg.STAGING.resolve()):
        shutil.rmtree(folder)


def dataset_dir(dataset_id: str) -> Path:
    return cfg.UPLOADS / safe_id(dataset_id)


def catalog():
    cfg.UPLOADS.mkdir(parents=True, exist_ok=True)
    result = []
    for path in cfg.UPLOADS.glob('*/dataset.json'):
        try:
            result.append(read_json(path))
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda item: item.get('created_at', ''), reverse=True)


def manifest(dataset_id: str):
    path = dataset_dir(dataset_id) / 'dataset.json'
    if not path.exists():
        raise ValueError('Dataset not found. Upload or select a ready dataset.')
    data = read_json(path)
    if data.get('status') != 'ready':
        raise ValueError('Dataset is not indexed. Re-ingest it after restarting the RAG server.')
    return data


def save_table(folder: Path, table: dict):
    frame = table['frame']
    schema = []
    for column in frame.columns:
        values = frame[column].dropna().tolist()
        if values and all(isinstance(v, (datetime, date, pd.Timestamp)) for v in values):
            kind = 'datetime'
        elif pd.api.types.is_bool_dtype(frame[column]):
            kind = 'boolean'
        elif pd.api.types.is_numeric_dtype(frame[column]):
            kind = 'number'
        else:
            kind = 'text' if not values or all(isinstance(v, str) for v in values) else 'mixed'
        schema.append({'name': str(column), 'type': kind})
    artifact = {k: v for k, v in table.items() if k != 'frame'}
    artifact['schema'] = schema
    artifact['records'] = frame.to_dict(orient='records')
    atomic_json(folder / f"table_{table['id']}.json", artifact)
    return {'table_id': table['id'], 'label': table['label'], 'sheet': table['sheet'],
            'rows': len(frame), 'columns': list(frame.columns), 'types': {c['name']: c['type'] for c in schema},
            'role': table.get('role'), 'searchable': table.get('searchable', True),
            'percentage_columns': table.get('percentage_columns', [])}


def load_table(dataset_id: str, table_id: str | None = None, *, allow_context=False):
    info = manifest(dataset_id)
    tables = info['tables']
    eligible = tables if allow_context else [t for t in tables if t['role'] not in {'workbook_index', 'index', 'directory'}]
    if table_id:
        selected = next((t for t in eligible if t['table_id'] == table_id), None)
        if selected is None:
            raise ValueError('Unknown table_id. Call get_dataset_info for candidates.')
    else:
        records = [t for t in eligible if t['role'] == 'records']
        choices = records or eligible
        if len(choices) != 1:
            raise ValueError('Choose table_id from: ' + ', '.join(f"{t['table_id']} ({t['label']})" for t in choices))
        selected = choices[0]
    # Only trusted IDs from the manifest participate in artifact paths.
    path = dataset_dir(dataset_id) / f"table_{selected['table_id']}.json"
    if not path.resolve().is_relative_to(dataset_dir(dataset_id).resolve()):
        raise ValueError('Invalid table artifact.')
    artifact = read_json(path)
    frame = pd.DataFrame(artifact['records'], columns=selected['columns'])
    for column in artifact['schema']:
        if column['type'] == 'datetime':
            frame[column['name']] = pd.to_datetime(frame[column['name']], errors='coerce')
        elif column['type'] == 'number':
            frame[column['name']] = pd.to_numeric(frame[column['name']], errors='coerce')
        elif column['type'] == 'boolean':
            frame[column['name']] = frame[column['name']].astype('boolean')
    return frame, artifact

