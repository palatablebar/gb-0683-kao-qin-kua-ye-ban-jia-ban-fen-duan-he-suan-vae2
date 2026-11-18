'use strict';
const sheet={pending:false};const message=document.getElementById('message');
async function recount(input){if(sheet.pending)return;sheet.pending=true;message.textContent='工时重算中…';try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(input)});const data=await response.json();if(!response.ok)throw new Error(data.message);location.reload();}catch(error){message.textContent=error.message+'，请修正打卡';}finally{sheet.pending=false;}}
document.getElementById('recount').addEventListener('click',()=>recount({sample:'dirty.csv'}));
const file=document.getElementById('clock-file');if(file)file.addEventListener('submit',event=>{event.preventDefault();const values=new FormData(file);const csv=values.get('csv');recount(csv?{csv}:{sample:values.get('sample')});});
const check=document.getElementById('check');if(check)check.addEventListener('click',async()=>{try{const response=await fetch('/api/runs/'+check.dataset.id+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:Number(check.dataset.version),decision:'通过'})});const data=await response.json();if(!response.ok)throw Error(data.message);check.dataset.version=data.review_version;message.textContent='工资复核通过 / '+data.review_version;}catch(error){message.textContent=error.message;}});
const paper=document.getElementById('paper');if(paper)paper.addEventListener('click',()=>window.print());
const close=document.getElementById('close');if(close)close.addEventListener('click',async()=>{
  if(close.disabled)return;
  close.disabled=true;message.textContent='封账中…';
  try{const response=await fetch('/api/periods/'+close.dataset.period+'/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:close.dataset.id,version:Number(close.dataset.version)})});const data=await response.json();if(!response.ok)throw Error(data.message);message.textContent='封账完成 / '+data.period+' / v'+data.review_version;location.reload();}catch(error){close.disabled=false;message.textContent=error.message+'，旧版证据保留';}});
document.querySelectorAll('td[tabindex]').forEach(cell=>cell.addEventListener('focus',()=>message.textContent=cell.textContent.trim()));
