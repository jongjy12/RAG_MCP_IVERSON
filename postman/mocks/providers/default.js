// Caller verification only. These responses do not establish live provider behavior.
const http = require('http');
const csv = 'Project,Task,Owner,Completion,survived,Planned,Actual\nBank,Debt API,A,0,1,2026-01-01,2026-01-03\nBank,Debt report,,50,0,2026-01-02,2026-01-02\nOther,Other task,B,100,1,2026-01-03,2026-01-02\n';
http.createServer(async (req,res) => {
  let body=req.body;
  if(body === undefined){let raw='';for await(const chunk of req) raw+=chunk;try{body=raw?JSON.parse(raw):{};}catch{body={};}}
  if(typeof body==='string'){try{body=JSON.parse(body);}catch{body={};}}
  const send=(code,value)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(value));};
  if(req.url==='/dataset.csv'){res.writeHead(200,{'Content-Type':'text/csv'});res.end(csv);return;}
  if(req.url==='/iris-page'){res.writeHead(200,{'Content-Type':'text/html'});res.end('<a href="/providers/iris.zip">Download</a>');return;}
  if(req.url==='/iris.zip' || req.url==='/kaggle-export.zip' || req.url==='/api/v1/datasets.DatasetApiService/DownloadDataset'){
    if(req.url==='/api/v1/datasets.DatasetApiService/DownloadDataset'){
      if(req.headers.authorization!=='Bearer fixture-token'){send(401,{code:401,message:'Invalid fixture token'});return;}
      if(body.datasetSlug==='forbidden'){send(403,{code:403,message:'Consent required'});return;}
      if(body.datasetSlug==='rate-limited'){send(429,{code:429,message:'Rate limited'});return;}
      if(body.ownerSlug!=='owner' || body.datasetSlug!=='iris'){send(501,{error:'Unmodeled dataset'});return;}
      res.writeHead(302,{'Location':'http://localhost:4010/providers/kaggle-export.zip'});res.end();return;
    }
    if(req.url==='/kaggle-export.zip' && req.headers.authorization){send(400,{error:'Credentials leaked across hosts'});return;}
    const bytes=Buffer.from('UEsDBBQAAAAIAOtUZV2cBrFlPQAAAGgQAAAJAAAAaXJpcy5kYXRh7cmxEYAgEETRnFqWHRGvCMswICByhrP/sQUK+C994abuUPOlw6fuNbPm+N58SnAcx3Ecx3Ecx3Ecx3n/flBLAwQUAAAACADrVGVdnAaxZT0AAABoEAAADwAAAGJlemRla0lyaXMuZGF0Ye3JsRGAIBBE0Zxalh0RrwjLMCAgcoaz/7EFCvgvfeGm7lDzpcOn7jWz5vjefEpwHMdxHMdxHMdxHMd5/35QSwMEFAAAAAgA61RlXef98pQSAAAAEAAAAAoAAABpcmlzLm5hbWVzS8usKCktSlXITS1JTEksSQQAUEsBAhQAFAAAAAgA61RlXZwGsWU9AAAAaBAAAAkAAAAAAAAAAAAAAIABAAAAAGlyaXMuZGF0YVBLAQIUABQAAAAIAOtUZV2cBrFlPQAAAGgQAAAPAAAAAAAAAAAAAACAAWQAAABiZXpkZWtJcmlzLmRhdGFQSwECFAAUAAAACADrVGVd5/3ylBIAAAAQAAAACgAAAAAAAAAAAAAAgAHOAAAAaXJpcy5uYW1lc1BLBQYAAAAAAwADAKwAAAAIAQAAAAA=','base64');
    res.writeHead(200,{'Content-Type':'application/zip'});res.end(bytes);return;
  }
  if(req.url==='/mcp'){
    const result=value=>send(200,{jsonrpc:'2.0',id:body.id,result:value});
    if(body.method==='initialize'){result({protocolVersion:body.params.protocolVersion,capabilities:{tools:{}},serverInfo:{name:'tavily-fixture',version:'1'}});return;}
    if(body.method==='notifications/initialized'){res.writeHead(202);res.end();return;}
    if(body.method==='tools/list'){result({tools:[{name:'tavily_search',description:'Find dataset CSVs',inputSchema:{type:'object',properties:{query:{type:'string'},max_results:{type:'integer',minimum:1,maximum:5},search_depth:{type:'string'}},required:['query']}}]});return;}
    if(body.method==='tools/call'){
      const query=body.params?.arguments?.query || '';
      const records={results:/iris/i.test(query)?[{title:'Iris dataset',url:'http://127.0.0.1:4010/providers/iris-page',content:'Synthetic Iris dataset page',score:0.9}]:[{title:'Bank tasks CSV',url:'https://github.com/example/demo/blob/main/tasks.csv',content:'Synthetic banking task dataset',score:0.9},{title:'Same tasks duplicate',url:'https://github.com/example/demo/blob/main/tasks.csv',content:'Duplicate evidence'}]};
      result({content:[{type:'text',text:JSON.stringify(records)}],structuredContent:records});return;
    }
    if(body.method==='ping'){result({});return;}
    if(req.method==='GET'){res.writeHead(405);res.end();return;}
    send(501,{error:'Unsupported mock MCP operation'});return;
  }
  if(req.url==='/v1/chat/completions'){
    const messages=body.messages || [], toolMessages=messages.filter(m=>m.role==='tool');
    let message;
    const tool=(name,args,id)=>({id,type:'function',function:{name,arguments:JSON.stringify(args)}});
    if(toolMessages.length===0) message={role:'assistant',content:null,tool_calls:[tool('get_dataset_info',{},'info')]};
    else if(toolMessages.length===1){
      const info=JSON.parse(toolMessages[0].content); const table=info.tables[0].table_id;
      message={role:'assistant',content:null,tool_calls:[
        tool('aggregate_data',{operation:'row_count',table_id:table,filters:[{column:'Project',operator:'eq',value:'Bank'}]},'count'),
        tool('rag_search_dataset',{query:'Explain Debt tasks',table_id:table,filters:[{column:'Project',operator:'eq',value:'Bank'}],top_k:2},'retrieve')]};
    }else message={role:'assistant',content:'The Bank project has 2 tasks (Pandas row_count). Retrieved records describe the Debt API and Debt report. Other project rows were excluded.'};
    send(200,{id:'fixture-chat',object:'chat.completion',created:1,model:body.model,choices:[{index:0,message,finish_reason:message.tool_calls?'tool_calls':'stop'}],usage:{prompt_tokens:1,completion_tokens:1,total_tokens:2}});return;
  }
  send(501,{error:'Endpoint outside the provider fixture contract'});
}).listen(process.env.PORT);

