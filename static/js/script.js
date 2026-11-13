'use strict';
const $ = id => document.getElementById(id);
const assetRoot = new URL('../', document.currentScript.src);
async function ensureMarkdown() {
  // Also support the running Flask process's already-cached HTML template,
  // without restarting it and losing the user's FAISS indexes/chat history.
  if (!document.querySelector('link[href$="/chat-markdown.css"]')) {
    const style=document.createElement('link'); style.rel='stylesheet';
    style.href=new URL('css/chat-markdown.css',assetRoot).href; document.head.append(style);
  }
  const load = path => new Promise((resolve,reject) => {
    const script=document.createElement('script'); script.src=new URL(path,assetRoot).href;
    script.onload=resolve; script.onerror=reject; document.head.append(script);
  });
  if (!window.marked) await load('vendor/marked/marked.umd.js');
  if (!window.DatasetMarkdown) await load('js/markdown.js');
}
let datasets = [], selectedId = null, chosenFile = null, searchId = null, candidateId = null;
let selectedCandidate = null, selectedFileInfo = null, inspectionId = null, fileId = null, downloadId = null, choosingDownload = false;
let preparing = false, searching = false, unknownCompletion = false, asking = false;
let connected = false, datasetViewVersion = 0;
let searchStep='find';
function setSearchStep(step) {
  searchStep=step;
  const review=step==='review';
  $('searchForm').hidden=review; $('searchIntro').hidden=review; $('candidates').hidden=review;
  $('backToResults').hidden=!review;
  $('searchStepLabel').textContent=review?'Step 2 of 2 · Review dataset':'Step 1 of 2 · Find a dataset';
  $('modalTitle').textContent=review?'Review dataset':'Find a dataset online';
  $('reviewActionHelp').hidden=!review;
  $('searchModal').querySelector('.search-modal-content').scrollTop=0;
}
const node = (tag, text, cls) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (cls) item.className = cls; return item; };
function status(id, text, kind='') { $(id).textContent=text; $(id).className=`status ${kind}`; }
function formatFileSize(bytes) {
  if (typeof bytes!=='number') return 'Size unavailable';
  const size=Number(bytes);
  if (!Number.isFinite(size) || size<0) return 'Size unavailable';
  if (size<1000) return `${size} B`;
  const units=['B','KB','MB','GB']; let amount=size, unit=0;
  while (amount>=1000 && unit<units.length-1) { amount/=1000; unit++; }
  return `${amount.toFixed(2)} ${units[unit]}`;
}
let activeProgress=null, progressVersion=0;
async function beginProgress(kind,target) {
  const data=await post('/preparation/start',{kind});
  const version=++progressVersion, box=$(target), started=Date.now();
  box.hidden=false; box.className='preparation-progress running';
  const stage=node('strong','Starting preparation…'), clock=node('span','0s');
  box.replaceChildren(stage,clock);
  activeProgress={version,box,stage,clock,started,kind};
  const tick=()=>{if(activeProgress?.version===version)clock.textContent=`Elapsed: ${Math.floor((Date.now()-started)/1000)}s`;};
  activeProgress.clockTimer=setInterval(tick,1000); tick();
  const poll=async()=>{
    if(activeProgress?.version!==version)return;
    try {const value=await api(`/preparation/${data.operation_id}`);if(activeProgress?.version===version)stage.textContent=value.stage;}
    catch {if(activeProgress?.version===version)stage.textContent='Waiting for preparation status…';}
    if(activeProgress?.version===version)activeProgress.pollTimer=setTimeout(poll,2000);
  };
  poll(); return data.operation_id;
}
function finishProgress(error) {
  if(!activeProgress)return;
  const p=activeProgress; activeProgress=null;clearInterval(p.clockTimer);clearTimeout(p.pollTimer);
  p.box.className=`preparation-progress ${error?(error.unknown?'unknown':'failed'):'complete'}`;
  p.stage.textContent=error?(error.unknown?'Completion unknown — do not retry automatically':'Preparation failed'):
    ({upload:'Dataset ready',use:'Dataset ready',inspect:'Files ready to review',download:'File sent to browser'})[p.kind];
}
function previewTable(columns,records,parent) {
  const wrap=node('div',undefined,'dataset-preview-scroll'),table=node('table'),head=node('thead'),header=node('tr');
  for(const column of columns){const th=node('th',String(column));th.scope='col';header.append(th);}head.append(header);table.append(head);
  const body=node('tbody');
  for(const record of records){const row=node('tr');for(const [index,column] of columns.entries()){
    const value=Array.isArray(record)?record[index]:record[column],text=value===null||value===undefined?'Missing':String(value),cell=node('td');
    if(text.length>120){const more=node('details');more.append(node('summary',text.slice(0,120)+'…'),node('p',text));cell.append(more);}else cell.textContent=text;
    row.append(cell);
  }body.append(row);}table.append(body);wrap.append(table);parent.append(wrap);
}
let previewDataset=null, previewRequest=0, previewLoadedKey=null;
function updateSuggestions() {
  const box=$('questionSuggestions');box.replaceChildren();if(!previewDataset)return;
  const table=previewDataset.tables.find(t=>t.table_id===$('previewTable').value);if(!table)return;
  const scope=previewDataset.tables.length>1?` In table “${table.label}”`:'',numeric=table.columns.find(c=>table.types?.[c]==='number'&&!/^(id|index|row[ _]?id)$|_id$/i.test(c));
  const questions=[`How many records are in this table?${scope}`,numeric?`What is the average of “${numeric}”?${scope}`:`How many distinct values are in “${table.columns[0]}”?${scope}`];
  box.append(node('p','Try a question','small muted'));
  for(const question of questions){const button=node('button',question,'suggestion');button.type='button';button.disabled=preparing||asking||unknownCompletion;button.addEventListener('click',()=>{if(preparing||asking||unknownCompletion)return;$('question').value=question;$('question').focus();});box.append(button);}
}
function setupPreview(info) {
  if(previewDataset?.dataset_id===info?.dataset_id)return;
  previewRequest++;previewLoadedKey=null;previewDataset=null;
  $('previewContent').replaceChildren();$('previewCaption').textContent='';$('previewTable').replaceChildren();$('dataPreview').hidden=!info;
  if(info){previewDataset={...info,tables:info.tables.filter(t=>!['workbook_index','index','directory'].includes(t.role))};
    for(const table of previewDataset.tables)$('previewTable').append(new Option(`${table.label} · ${table.rows.toLocaleString()} rows`,table.table_id));
    if(!previewDataset.tables.length){$('previewCaption').textContent='No previewable tables.';}
    else if($('dataPreview').open)loadPreview();
  }
  updateSuggestions();
}
async function loadPreview() {
  if(preparing||asking||unknownCompletion||!previewDataset||!$('dataPreview').open||!$('previewTable').value)return;
  const id=previewDataset.dataset_id,tableId=$('previewTable').value,key=`${id}:${tableId}`;
  if(previewLoadedKey===key)return;
  const version=++previewRequest;previewLoadedKey=key;$('previewContent').replaceChildren();$('previewCaption').textContent='Loading preview…';
  try{const data=await api(`/datasets/preview?dataset_id=${encodeURIComponent(id)}&table_id=${encodeURIComponent(tableId)}`);
    if(version!==previewRequest||id!==selectedId)return;
    const table=previewDataset.tables.find(t=>t.table_id===tableId);
    $('previewCaption').textContent=`${previewDataset.name} · ${table.columns.length} columns · Showing the first ${data.records.length} of ${data.total_matches.toLocaleString()} rows`;
    previewTable(table.columns,data.records,$('previewContent'));
  }catch(error){if(version===previewRequest){previewLoadedKey=null;$('previewCaption').textContent=error.message;}}
}
$('dataPreview').addEventListener('toggle',()=>{if($('dataPreview').open)loadPreview();});
$('previewTable').addEventListener('change',()=>{updateSuggestions();loadPreview();});
function cleanCandidateSummary(value) {
  // Search descriptions are cleaned by the server and inserted as text.
  return String(value || 'Dataset source').trim();
}
function downloadFact(label,value) {
  const fact=node('div',undefined,'download-fact');
  fact.append(node('span',label),node('strong',value));
  return fact;
}
function summaryRows(info) {
  if (!Number.isFinite(info?.row_count) || !Number.isFinite(info?.column_count)) return null;
  return {
    rows: ['present','absent'].includes(info.header)?Math.max(0,info.row_count-(info.header==='present'?1:0)):null,
    columns: info.column_count
  };
}
function selectedFileSummary(info) {
  const card=node('section',undefined,'download-summary-card selected-file-summary');
  const heading=node('div',undefined,'download-summary-heading');
  heading.append(node('span','▤','download-check'));
  const title=node('div');
  title.append(node('h3',info.name || 'Dataset file','download-title'));
  heading.append(title); card.append(heading);
  try {
    const url=new URL(selectedCandidate?.page_url || selectedCandidate?.url);
    if (['http:','https:'].includes(url.protocol)) {
      const source=node('a',selectedCandidate?.source || url.hostname,'small');
      source.href=url.href; source.target='_blank'; source.rel='noopener noreferrer'; card.append(source);
    }
  } catch { /* Source unavailable. */ }
  const facts=node('div',undefined,'download-facts');
  facts.append(downloadFact('FORMAT',info.format),downloadFact('FILE SIZE',formatFileSize(info.size_bytes)));
  const counts=summaryRows(info);
  if (counts) {
    const rows=downloadFact('ROWS',counts.rows===null?'Choose header setting':counts.rows.toLocaleString()); rows.lastElementChild.id='selectedFileRows';
    facts.append(rows,downloadFact('COLUMNS',counts.columns.toLocaleString()));
  }
  card.append(facts,node('p','✓ File verified','small ready'));
  return card;
}
function showDownloadSummary(info,downloadedBytes) {
  const panel=$('inspectionPanel'); panel.hidden=false; panel.replaceChildren();
  const card=node('section',undefined,'download-summary-card');
  const heading=node('div',undefined,'download-summary-heading');
  heading.append(node('span','✓','download-check'));
  const title=node('div'); title.append(node('p','DOWNLOAD COMPLETE','eyebrow'),node('h3',selectedCandidate?.name || 'Dataset file','download-title'));
  heading.append(title); card.append(heading);
  const facts=node('div',undefined,'download-facts');
  facts.append(downloadFact('FILE',info?.name || 'Dataset file'));
  facts.append(downloadFact('FORMAT',info?.format || selectedCandidate?.format || 'File'));
  facts.append(downloadFact('SIZE',formatFileSize(downloadedBytes)));
  const counts=summaryRows(info);
  if (counts) facts.append(downloadFact('ROWS',counts.rows.toLocaleString()),downloadFact('COLUMNS',counts.columns.toLocaleString()));
  card.append(facts,node('p','Saved to your browser’s downloads. This file was not added to the active dataset or indexed.','download-note'));
  panel.append(card);
}
async function api(path, options={}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) { const error = new Error(data.error || 'Request failed.'); error.unknown = !!data.unknown_completion; throw error; }
  return data;
}
const post = (path, data) => api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
function controls() {
  for(const button of $('questionSuggestions').querySelectorAll('button'))button.disabled=preparing||asking||unknownCompletion;
  $('previewTable').disabled=preparing||asking||unknownCompletion;
  if(!preparing&&!asking&&!unknownCompletion&&$('dataPreview').open)loadPreview();
  $('uploadButton').disabled = !chosenFile || preparing || asking || unknownCompletion;
  $('file').disabled = preparing || asking || unknownCompletion;
  $('findDataset').disabled = preparing || asking;
  $('datasetPicker').disabled = preparing || asking;
  $('sendButton').disabled = preparing || asking;
  $('question').disabled = preparing || asking;
  $('searchButton').disabled = searching || preparing || unknownCompletion;
  $('searchLimit').disabled = searching || preparing || unknownCompletion;
  $('searchTopic').disabled = searching || preparing;
  const incomplete = (inspectionId && (!fileId || (selectedFileInfo && ['CSV','DATA'].includes(selectedFileInfo.format) && !['present','absent'].includes(selectedFileInfo.header)))) || (choosingDownload && !downloadId);
  $('useDataset').disabled = !candidateId || preparing || searching || unknownCompletion || incomplete;
  $('downloadOnly').disabled = !candidateId || preparing || searching || unknownCompletion || incomplete;
  $('downloadOnly').hidden = !inspectionId;
  $('useDataset').textContent = inspectionId ? 'Use dataset' : 'Review';
  $('downloadOnly').textContent = 'Download only';
  $('backToResults').disabled=preparing || searching;
  $('closeModal').disabled = preparing;
  for (const radio of $('candidates').querySelectorAll('input')) radio.disabled=preparing || searching || unknownCompletion;
  for (const radio of $('inspectionPanel').querySelectorAll('input,select')) radio.disabled=preparing || searching || unknownCompletion;
}
function details(info) {
  const box=$('datasetDetails');
  const wasOpen=box.querySelector('.metadata-disclosure')?.open || false;
  box.replaceChildren();
  if (!info) { box.append(node('p','Choose a ready dataset or add a file.','muted small')); return; }
  const metadata=node('table',undefined,'dataset-metadata');
  metadata.append(node('caption','Dataset details'));
  const body=node('tbody');
  const addRow=(label,value)=>{
    const row=node('tr'),heading=node('th',label),cell=node('td');
    heading.scope='row';
    if (value instanceof Node) cell.append(value); else cell.textContent=String(value);
    row.append(heading,cell); body.append(row);
  };
  addRow('Name',info.name);
  addRow('Format',info.format.toUpperCase());
  addRow('Status',info.status==='ready'?'Ready':'Re-upload required');
  addRow('Rows',info.rows.toLocaleString());
  addRow('Unique columns',(info.columns || [...new Set(info.tables.flatMap(table=>table.columns))]).length.toLocaleString());
  addRow('Tables',info.tables.length.toLocaleString());
  const source=info.source || {};
  let sourceLink=null;
  try {
    const url=new URL(source.page_url);
    if (['http:','https:'].includes(url.protocol)) {
      sourceLink=node('a',url.hostname); sourceLink.href=url.href;
      sourceLink.target='_blank'; sourceLink.rel='noopener noreferrer';
      sourceLink.title=url.href;
    }
  } catch { /* A local upload has no source URL. */ }
  addRow('Source',sourceLink || (Object.keys(source).length?'Not available':'Local upload'));
  if (source.selected_file) addRow('Source file',source.selected_file);
  const preparedAt=new Date(info.created_at);
  if (!Number.isNaN(preparedAt.getTime())) addRow('Prepared',preparedAt.toLocaleString());
  const disclosure=node('details',undefined,'metadata-disclosure');
  disclosure.open=wasOpen;
  disclosure.append(node('summary','Dataset details & source'));
  metadata.append(body); disclosure.append(metadata); box.append(disclosure);
  for (const warning of info.warnings || []) box.append(node('p',warning,'warning'));
}
async function refreshDatasets() {
  const version=datasetViewVersion;
  const data=await api('/datasets');
  if (version!==datasetViewVersion) return;
  datasets=data.datasets.filter(info=>info.status==='ready');
  selectedId=datasets.some(info=>info.dataset_id===data.selected_dataset_id)?data.selected_dataset_id:null;
  $('datasetPicker').replaceChildren(new Option('Choose a dataset', ''));
  for (const info of datasets) $('datasetPicker').append(new Option(info.name,info.dataset_id));
  $('datasetPicker').value=selectedId || ''; details(datasets.find(x=>x.dataset_id===selectedId));
  setupPreview(datasets.find(x=>x.dataset_id===selectedId)); controls();
}
function clearDatasetSelection() {
  datasetViewVersion++;
  datasets=[]; selectedId=null;
  $('datasetPicker').replaceChildren(new Option('Choose a dataset', ''));
  details(null); setupPreview(null); controls();
}
async function checkConnection() {
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),15000);
  try {
    const info=await api('/health',{signal:controller.signal});
    if (!info.local_servers?.rag || !info.local_servers?.pandas) throw new Error('Local tools unavailable');
    $('connectionStatus').textContent='Local tools connected';
    const previousSelected=selectedId;
    await refreshDatasets();
    if (!connected || previousSelected!==selectedId) {
      await chatHistory();
    }
    connected=true;
  } catch (error) {
    connected=false;
    $('connectionStatus').textContent='Local service unavailable';
    clearDatasetSelection();
  } finally {
    clearTimeout(timeout);
    setTimeout(checkConnection,5000);
  }
}
function resultTable(records, parent) {
  if (!records?.length) return;
  const wrap=node('div',undefined,'result-table-wrapper'), table=node('table');
  const columns=Object.keys(records[0]);
  const header=node('tr'); for (const column of columns) header.append(node('th',column));
  table.append(header);
  for (const record of records) { const row=node('tr'); for (const column of columns) row.append(node('td',record[column]===null?'Missing':typeof record[column]==='object'?JSON.stringify(record[column]):String(record[column] ?? ''))); table.append(row); }
  wrap.append(table); parent.append(wrap);
}
function showAnswer(result) {
  const message=node('article',undefined,'message assistant');
  message.append(node('div',`ANALYST · ${result.web_search ? 'Web search' : (result.dataset_name || 'Selected dataset')}`,'answer-label'),DatasetMarkdown.render(result.answer));
  if (result.sources?.length) {
    const sources=node('div',undefined,'web-sources'); sources.append(node('strong','Sources'));
    for(const source of result.sources){
      try { const url=new URL(source.url); if(!['http:','https:'].includes(url.protocol)||url.username||url.password)continue;
        const link=node('a',source.title||url.hostname);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';sources.append(link);
      } catch { /* Ignore malformed source URLs. */ }
    }
    message.append(sources);
  }
  if (result.tool_calls?.length) {
    const count=result.tool_calls.length;
    const evidence=node('details',undefined,'evidence'); evidence.append(node('summary',`${count} tool ${count===1?'call':'calls'} · calculations & sources`));
    for (const call of result.tool_calls) {
      const entry=node('details'); entry.append(node('summary',call.tool),node('pre',JSON.stringify(call.arguments,null,2)));
      resultTable(call.result?.records,entry);
      entry.append(node('pre',JSON.stringify(call.result,null,2))); evidence.append(entry);
    }
    message.append(evidence);
  }
  $('chatFeed').append(message); $('chatFeed').scrollTop=$('chatFeed').scrollHeight;
}
async function chatHistory() {
  const data=await api('/chat/history'); $('chatFeed').replaceChildren();
  if (!data.messages.length) {
    const empty=node('div',undefined,'empty-state'); empty.append(node('strong',selectedId?'What would you like to know?':'Start with a dataset.'),node('p',selectedId?'Try “How many records are there?” or compare groups.':'Upload a file, find a dataset, or ask /websearch followed by a question.')); $('chatFeed').append(empty);
  }
  for (const record of data.messages) { $('chatFeed').append(node('div',record.question,'message user')); showAnswer(record); }
}
function chooseFile(file) {
  if (preparing || unknownCompletion) return;
  chosenFile=file;
  $('chosenFile').textContent=file?`${file.name} · ${formatFileSize(file.size)}`:'CSV, XLSX, XLS · up to 10 MiB';
  controls();
}
$('file').addEventListener('change',()=>chooseFile($('file').files[0]));
for (const event of ['dragover','dragleave','drop']) $('dropZone').addEventListener(event,e=>{e.preventDefault(); $('dropZone').classList.toggle('dragging',event==='dragover'); if(event==='drop') chooseFile(e.dataTransfer.files[0]);});
$('uploadForm').addEventListener('submit',async e=>{
  e.preventDefault(); if (!chosenFile || preparing) return;
  preparing=true; controls(); status('uploadStatus','Preparing dataset - uploading and indexing…');
  try {
    const op=await beginProgress('upload','uploadProgress');
    const form=new FormData(); form.append('file',chosenFile);form.append('operation_id',op);
    await api('/upload',{method:'POST',body:form}); await refreshDatasets(); await chatHistory();
    status('uploadStatus','Dataset ready. Ask a question below.','success');
  } catch(error) { finishProgress(error); unknownCompletion=!!error.unknown; status('uploadStatus',error.message,'error'); }
  finally { finishProgress(); preparing=false; controls(); }
});
$('datasetPicker').addEventListener('change',async()=>{
  if (!$('datasetPicker').value) { $('datasetPicker').value=selectedId || ''; return; }
  try { await post('/datasets/select',{dataset_id:$('datasetPicker').value}); await refreshDatasets(); await chatHistory(); }
  catch(error) { status('uploadStatus',error.message,'error'); $('datasetPicker').value=selectedId || ''; }
});
$('findDataset').addEventListener('click',()=>{$('searchModal').showModal(); $('searchTopic').focus(); controls();});
function clearInspection(hideProgress=true) {
  const old=inspectionId;
  inspectionId=null; fileId=null; selectedFileInfo=null; downloadId=null; choosingDownload=false;
  $('inspectionPanel').hidden=true; $('inspectionPanel').replaceChildren();
  if(hideProgress)$('searchProgress').hidden=true;
  if (old) post('/dataset-search/cancel-inspection',{inspection_id:old}).catch(()=>{});
  setSearchStep('find');
  controls();
}
$('backToResults').addEventListener('click',()=>{if(preparing||searching)return;clearInspection();status('searchStatus','Select a result to review.');$('useDataset').focus();});
$('closeModal').addEventListener('click',()=>{clearInspection(); $('searchModal').close(); $('findDataset').focus();});
$('searchModal').addEventListener('cancel',e=>{if(preparing)e.preventDefault(); else clearInspection();});
$('searchForm').addEventListener('submit',async e=>{
  e.preventDefault(); clearInspection(); selectedCandidate=null; searching=true; candidateId=null; controls(); status('searchStatus','Searching public datasets…');
  try {
    const data=await post('/dataset-search',{topic:$('searchTopic').value,limit:Number($('searchLimit').value)});
    searchId=data.search_id; $('candidates').replaceChildren();
    if(!data.candidates.length) $('candidates').append(node('p','No dataset links found. Try a more specific topic or source.','muted empty-search'));
    for(const candidate of data.candidates) {
      const label=node('label',undefined,'candidate'), radio=node('input'); radio.type='radio'; radio.name='datasetCandidate'; radio.value=candidate.id;
      radio.addEventListener('change',()=>{clearInspection(); candidateId=candidate.id;selectedCandidate=candidate;controls();});
      const body=node('div',undefined,'candidate-body'); body.append(node('h3',candidate.name));
      const badges=node('div',undefined,'candidate-badges');
      badges.append(node('span',candidate.format==='PAGE'?'Dataset page':`${candidate.format} · Not checked`,'candidate-badge'));
      if (candidate.source) badges.append(node('span',candidate.source,'candidate-badge'));
      body.append(badges);
      const summary=cleanCandidateSummary(candidate.description);
      const shortSummary=summary.length>360?summary.slice(0,360).trimEnd()+'…':summary;
      const description=node('p',shortSummary,'candidate-summary');
      if (summary.length>360) { const more=node('details',undefined,'candidate-more'); more.append(node('summary','More details'),node('p',summary)); body.append(description,more); }
      else body.append(description);
      const url=candidate.url || candidate.download_url;
      if(/^https?:\/\//i.test(url)){const link=node('a',candidate.source || new URL(url).hostname);link.href=url;link.target='_blank';link.rel='noopener noreferrer';body.append(link);}
      label.append(radio,body); $('candidates').append(label);
    }
    status('searchStatus',data.warnings.length?data.warnings.join(' '):`${data.candidates.length} dataset results found.`);
  } catch(error) { status('searchStatus',error.message,'error'); }
  finally { searching=false; controls(); }
});
function selectionBody() {
  return {search_id:searchId,candidate_id:candidateId,inspection_id:inspectionId,file_id:fileId,header:$('fileHeader')?.value || null};
}
function inspectionProgress() {
  const url=selectedCandidate?.page_url || selectedCandidate?.url || '';
  return /^https:\/\/(www\.)?kaggle\.com\/datasets\//i.test(url)
    ? 'Downloading Kaggle archive and inspecting files…' : 'Inspecting dataset files…';
}
async function inspectSelection() {
  setSearchStep('review');
  const op=await beginProgress('inspect','searchProgress');
  const data=await post('/dataset-search/inspect',{search_id:searchId,candidate_id:candidateId,download_id:downloadId,operation_id:op});
  const panel=$('inspectionPanel'); panel.hidden=false; panel.replaceChildren();
  if (data.downloads) {
    choosingDownload=true; downloadId=null;
    panel.append(node('p','Choose a download to review.','small'));
    for (const item of data.downloads) {
      const label=node('label',undefined,'candidate'),radio=node('input'); radio.type='radio'; radio.name='downloadChoice';
      radio.addEventListener('change',()=>{downloadId=item.id;controls();});
      label.append(radio,node('span',`${item.name} · ${item.format}`)); panel.append(label);
    }
    status('searchStatus','Select a download, then click Review.'); controls(); return;
  }
  choosingDownload=false; downloadId=null; inspectionId=data.inspection_id; fileId=null;
  if (data.source_format==='ZIP') panel.append(node('p',`Archive size: ${formatFileSize(data.source_size_bytes)}`,'file-size-summary'));
  const headerBox=node('div');
  const choose=item=>{
    fileId=item.id; selectedFileInfo={name:item.name,format:item.format,size_bytes:item.size_bytes,row_count:item.row_count,column_count:item.column_count,header:item.header}; headerBox.replaceChildren();
    headerBox.append(selectedFileSummary(selectedFileInfo));
    if (item.preview?.length) {
      if(item.header_message)headerBox.append(node('p',item.header_message,item.header_detection==='uncertain'?'warning':'small muted'));
      const previewDetails=node('details',undefined,'file-preview');
      previewDetails.open=!item.header;
      previewDetails.append(node('summary','Preview dataset'));
      const preview=node('div');previewDetails.append(node('p','Sample preview · up to 20 columns','muted small'),preview);headerBox.append(previewDetails);
      const renderSample=header=>{preview.replaceChildren();const cols=header==='present'?item.preview[0]:item.preview[0].map((_,i)=>`Column ${i+1}`),rows=header==='present'?item.preview.slice(1):item.preview;previewTable(cols,rows,preview);};
      renderSample(item.header);
      const label=node('label','Column names: '); label.className='header-choice'; const select=node('select'); select.id='fileHeader';
      if(!item.header)select.append(new Option('Choose a header setting…',''));
      select.append(new Option('First row contains column names','present'),new Option('No header: keep first row as data','absent'));
      select.value=item.header || '';
      select.addEventListener('change',()=>{selectedFileInfo.header=select.value;renderSample(select.value);const counts=summaryRows(selectedFileInfo);if(counts&&$('selectedFileRows'))$('selectedFileRows').textContent=counts.rows===null?'Choose header setting':counts.rows.toLocaleString();controls();});
      label.append(select); headerBox.append(label);
    }
    controls();
  };
  if (data.files.length===1) choose(data.files[0]);
  else {
    const label=node('label','Data file','review-file-label'),select=node('select');
    select.id='reviewFile';label.htmlFor=select.id;
    select.append(new Option('Choose a file…',''));
    for (const item of data.files) select.append(new Option(`${item.name} · ${item.format} · ${formatFileSize(item.size_bytes)}`,item.id));
    select.addEventListener('change',()=>{const item=data.files.find(file=>file.id===select.value);if(item)choose(item);else{fileId=null;selectedFileInfo=null;headerBox.replaceChildren();controls();}});
    panel.append(label,select);
  }
  panel.append(headerBox); status('searchStatus',data.files.length===1?'Review the dataset below.':'Choose a file to review.'); controls();
}
$('useDataset').addEventListener('click',async()=>{
  preparing=true; controls(); status('searchStatus',!inspectionId?`${inspectionProgress()} Checking file sizes…`:'Preparing dataset - downloading and indexing…');
  try {
    if (!inspectionId) {await inspectSelection(); return;}
    const op=await beginProgress('use','searchProgress');
    await post('/dataset-search/use',{...selectionBody(),operation_id:op});
    inspectionId=null; fileId=null; selectedFileInfo=null; $('inspectionPanel').hidden=true; $('inspectionPanel').replaceChildren();
    await refreshDatasets(); await chatHistory(); setSearchStep('find'); $('searchModal').close(); $('datasetDetails').focus();
    status('uploadStatus','Online dataset ready and selected.','success'); status('searchStatus','Dataset ready.','success');
  } catch(error) { finishProgress(error); if (inspectionId) clearInspection(false); unknownCompletion=!!error.unknown; status('searchStatus',error.message,'error'); }
  finally { finishProgress(); preparing=false; if(inspectionId||choosingDownload||!$('searchModal').open)$('searchProgress').hidden=true; controls(); }
});
$('downloadOnly').addEventListener('click',async()=>{
  if (!inspectionId) return;
  preparing=true; controls(); status('searchStatus','Downloading file…');
  try {
    const op=await beginProgress('download','searchProgress');
    const response=await fetch('/dataset-search/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...selectionBody(),operation_id:op})});
    if(!response.ok) throw new Error((await response.json()).error || 'Download failed.');
    const downloadedInfo=selectedFileInfo?{...selectedFileInfo,header:$('fileHeader')?.value || selectedFileInfo.header}:null;
    inspectionId=null; fileId=null; $('inspectionPanel').hidden=true; $('inspectionPanel').replaceChildren();
    const blob=await response.blob(), url=URL.createObjectURL(blob), link=node('a'); link.href=url;
    const disposition=response.headers.get('Content-Disposition') || '';
    link.download=disposition.match(/filename="?([^";]+)"?/)?.[1] || 'dataset.csv';
    document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    showDownloadSummary(downloadedInfo,blob.size);
    status('searchStatus','File downloaded. The active dataset has not changed.','success');
  } catch(error) { finishProgress(error); if (inspectionId) clearInspection(false); status('searchStatus',error.message,'error'); }
  finally { finishProgress(); preparing=false; controls(); }
});
$('askForm').addEventListener('submit',async e=>{
  e.preventDefault(); const question=$('question').value.trim(); if(!question || asking)return;
  if(!selectedId && !/^\/websearch(?:\s|$)/i.test(question)){status('chatStatus','Select a dataset or use /websearch followed by a question.','error');return;}
  const requestDataset=selectedId; asking=true; controls(); $('chatFeed').querySelector('.empty-state')?.remove();
  $('chatFeed').append(node('div',question,'message user')); $('question').value='';status('chatStatus',/^\/websearch(?:\s|$)/i.test(question)?'Searching the web…':'Thinking and checking your data…');
  try { const result=await post('/ask',{question,dataset_id:requestDataset}); showAnswer(result); status('chatStatus',''); }
  catch(error){status('chatStatus',error.message,'error');}
  finally{asking=false;controls();$('question').focus();}
});
(async()=>{
  try { await ensureMarkdown(); }
  catch { window.DatasetMarkdown={render:text=>node('p',text,'answer-text markdown-fallback')}; }
  await checkConnection();
})();
