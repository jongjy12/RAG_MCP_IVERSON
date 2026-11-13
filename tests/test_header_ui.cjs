// Exercise the actual inspection rendering and action guards without providers.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const roots=new Map();
class Element {
  constructor(tag,text=''){this.tagName=tag;this.textContent=text;this.children=[];this.events={};this.value='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  addEventListener(name,fn){this.events[name]=fn;}
  querySelector(){return this;}
  focus(){}
  querySelectorAll(selector){return this.children.flatMap(c=>[c,...c.querySelectorAll(selector)]).filter(c=>selector.split(',').includes(c.tagName));}
  get lastElementChild(){return this.children.at(-1);}
}
const find=(item,id)=>item.id===id?item:item.children.map(c=>find(c,id)).find(Boolean);
const document={currentScript:{src:'http://127.0.0.1:5000/static/js/script.js'},
  createElement:tag=>new Element(tag),getElementById(id){
    for(const root of roots.values()){const child=find(root,id);if(child)return child;}
    if(!roots.has(id))roots.set(id,new Element('div'));return roots.get(id);
  }};
const context={document,URL,Option:function(text,value){return Object.assign(new Element('option',text),{value});},window:{},console};
vm.createContext(context);
let source=fs.readFileSync(require('node:path').join(__dirname,'../static/js/script.js'),'utf8');
source=source.slice(0,source.lastIndexOf('(async()=>{'));
vm.runInContext(source,context);
async function inspect(item){
  context.fixture={inspection_id:'inspection',source_format:'CSV',files:[item]};
  await vm.runInContext(`
    beginProgress=async()=> 'operation';api=async()=>fixture;
    selectedCandidate={name:'Example',format:'CSV',description:'Measurements < 5 and > 1; ~150 samples; -10 degrees.'};candidateId='c1';
    inspectSelection();`,context);
}
(async()=>{
  assert.equal(vm.runInContext(`cleanCandidateSummary('Measurements < 5 and > 1; ~150 samples; -10 degrees.')`,context),
    'Measurements < 5 and > 1; ~150 samples; -10 degrees.');
  context.calls=[];
  context.fixture={downloads:[{id:'d1',name:'data.zip',format:'ZIP'}]};
  vm.runInContext(`
    beginProgress=async()=> 'operation';finishProgress=()=>{};
    api=async(path,options)=>{calls.push({path,body:JSON.parse(options.body)});return fixture;};
    selectedCandidate={name:'Example',format:'PAGE'};candidateId='page';controls();`,context);
  const use=document.getElementById('useDataset'),download=document.getElementById('downloadOnly');
  assert.equal(use.textContent,'Review');
  assert.equal(download.hidden,true);
  await use.events.click();
  assert.equal(use.textContent,'Review');
  assert.equal(document.getElementById('candidates').hidden,true);
  assert.equal(document.getElementById('searchForm').hidden,true);
  assert.equal(document.getElementById('backToResults').hidden,false);
  assert.equal(use.disabled,true);
  assert.equal(download.hidden,true);
  document.getElementById('inspectionPanel').querySelectorAll('input')[0].events.change();
  assert.equal(use.disabled,false);
  context.fixture={inspection_id:'inspection',source_format:'ZIP',source_size_bytes:100,
    files:[{id:'f-xlsx',name:'data.xlsx',format:'XLSX',size_bytes:80,preview:[]}]};
  await use.events.click();
  assert.deepEqual(context.calls.map(call=>call.path),['/dataset-search/inspect','/dataset-search/inspect']);
  assert.equal(context.calls[1].body.download_id,'d1');
  assert.equal(download.hidden,false);
  assert.equal(download.textContent,'Download only');
  assert.equal(use.textContent,'Use dataset');
  assert.equal(download.disabled,false);
  assert.equal(use.disabled,false);
  vm.runInContext(`inspectionId=null;choosingDownload=false;selectedCandidate={format:'CSV'};controls();`,context);
  assert.equal(use.textContent,'Review');
  assert.equal(download.hidden,true);
  await inspect({id:'f1',name:'cities.csv',format:'CSV',size_bytes:50,row_count:3,column_count:2,
    header:null,header_detection:'uncertain',header_message:'Review header',preview:[['name','city'],['Alice','London'],['Bob','Paris']]});
  assert.equal(document.getElementById('useDataset').disabled,true);
  assert.equal(document.getElementById('downloadOnly').disabled,true);
  assert.equal(document.getElementById('selectedFileRows').textContent,'Choose header setting');
  const panel=document.getElementById('inspectionPanel');
  assert.equal(panel.querySelectorAll('h3')[0].textContent,'cities.csv');
  assert.equal(panel.querySelectorAll('input').length,0);
  assert.equal(panel.querySelectorAll('details')[0].open,true);
  const select=document.getElementById('fileHeader');
  assert.equal(select.value,'');select.value='absent';select.events.change();
  assert.equal(document.getElementById('useDataset').disabled,false);
  assert.equal(document.getElementById('downloadOnly').disabled,false);
  assert.equal(document.getElementById('selectedFileRows').textContent,'3');
  select.value='present';select.events.change();
  assert.equal(document.getElementById('selectedFileRows').textContent,'2');
  select.value='';select.events.change();
  assert.equal(document.getElementById('useDataset').disabled,true);
  await inspect({id:'f2',name:'iris.csv',format:'CSV',size_bytes:4700,row_count:150,column_count:5,
    header:'absent',header_detection:'likely_absent',preview:[['5.1','3.5','1.4','0.2','Iris-setosa'],['4.9','3','1.4','.2','Iris-setosa']]});
  assert.equal(document.getElementById('selectedFileRows').textContent,'150');
  assert.equal(document.getElementById('useDataset').disabled,false);
  assert.equal(document.getElementById('inspectionPanel').querySelectorAll('details')[0].open,false);
  context.fixture={inspection_id:'multi',files:[{id:'one',name:'one.xlsx',format:'XLSX',size_bytes:80},{id:'two',name:'two.xlsx',format:'XLSX',size_bytes:90}]};
  await vm.runInContext('inspectSelection()',context);
  assert.equal(use.disabled,true);
  const filePicker=document.getElementById('reviewFile');filePicker.value='two';filePicker.events.change();
  assert.equal(vm.runInContext('fileId',context),'two');assert.equal(use.disabled,false);
  document.getElementById('backToResults').events.click();
  assert.equal(document.getElementById('candidates').hidden,false);
  assert.equal(document.getElementById('searchForm').hidden,false);
  assert.equal(document.getElementById('inspectionPanel').hidden,true);
  assert.equal(vm.runInContext('candidateId',context),'c1');
  console.log('Header UI checks passed: uncertain choices block both actions; preview/count/override and headerless defaults work.');
})().catch(error=>{console.error(error);process.exitCode=1;});
