// TuyaSync front-end. Talks to the FastAPI backend on the same origin (ingress).
const API = (p) => `.${p}`;  // ingress serves under a base path; relative keeps it working
let STATE = { devices:[], snapshot:[], ha_entries:[], mismatches:[], creds_configured:false };
let view = 'mismatches';
let sortKey = null, sortDir = 1, catFilter = 'all';
const el = id => document.getElementById(id);
const toast = el('toast'); let tT;
function showToast(m){ toast.textContent=m; toast.classList.add('show'); clearTimeout(tT); tT=setTimeout(()=>toast.classList.remove('show'),1100); }
function esc(s){ return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function copy(t,n){ navigator.clipboard.writeText(t).then(()=>{ n.classList.add('copied'); setTimeout(()=>n.classList.remove('copied'),800); showToast('Copied: '+(t.length>24?t.slice(0,24)+'…':t)); }); }
function ipNum(ip){ if(!ip)return -1; const p=ip.split('.').map(Number); if(p.length!==4||p.some(isNaN))return -1; return ((p[0]*256+p[1])*256+p[2])*256+p[3]; }
function banner(msg,kind){ const b=el('banner'); if(!msg){b.className='banner';return;} b.textContent=msg; b.className='banner show '+(kind||'warn'); }
function fmtTime(t){ return t? new Date(t*1000).toLocaleTimeString():'—'; }

async function loadState(){
  const r = await fetch(API('/api/state')); STATE = await r.json();
  if(!STATE.creds_configured) banner('Cloud credentials not set — add API key/secret/device-id in the add-on Configuration tab to enable Sync.','warn');
  else banner('');
  render();
}

async function doAction(btn, path, okMsg){
  btn.classList.add('loading'); btn.disabled=true;
  try{
    const r = await fetch(API(path), {method:'POST'});
    const j = await r.json();
    if(!r.ok) throw new Error(j.detail||'request failed');
    showToast(okMsg||'Done');
    await loadState();
  }catch(e){ banner(e.message,'err'); }
  finally{ btn.classList.remove('loading'); btn.disabled=false; }
}

el('syncBtn').onclick = ()=>doAction(el('syncBtn'),'/api/sync','Cloud synced');
el('scanBtn').onclick = ()=>doAction(el('scanBtn'),'/api/scan','LAN scanned');
el('haBtn').onclick   = ()=>doAction(el('haBtn'),'/api/ha/refresh','HA refreshed');

async function fixOne(m, btn){
  btn.disabled=true; btn.textContent='Fixing…';
  try{
    const r = await fetch(API('/api/fix'),{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({entry_id:m.entry_id,new_host:m.scanned_ip,local_key:m.local_key,
        protocol_version:m.protocol_version,poll_only:m.poll_only})});
    const j = await r.json();
    if(!r.ok) throw new Error(j.detail||'fix failed');
    btn.textContent='✓ Fixed'; btn.classList.add('done');
    showToast(`${m.title} → ${m.scanned_ip}`);
    await loadState();
  }catch(e){ banner(e.message,'err'); btn.disabled=false; btn.textContent='Fix'; }
}

// ---- rendering ----
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active'); view=t.dataset.view; sortKey=null; catFilter='all'; render();
});
el('search').oninput = ()=>{ el('searchClear').classList.toggle('hidden',!el('search').value); render(); };
el('searchClear').onclick = ()=>{ el('search').value=''; el('searchClear').classList.add('hidden'); render(); el('search').focus(); };
el('revealBtn').onclick = ()=>{ const on=document.body.classList.toggle('reveal');
  el('revealBtn').classList.toggle('armed',on); el('revealLabel').textContent=on?'Hide keys':'Reveal keys'; };

function renderStats(){
  const s=el('stats');
  if(view==='mismatches'){
    const mm=STATE.mismatches.filter(m=>m.mismatch).length;
    const onlan=STATE.mismatches.filter(m=>m.found_on_lan).length;
    const off=STATE.ha_entries.filter(e=>e.state==='setup_retry').length;
    s.innerHTML=`
      <div class="stat ${catFilter==='all'?'active':''}" data-f="all"><span class="n">${STATE.ha_entries.length}</span><span class="l">HA Devices</span></div>
      <div class="stat mm ${catFilter==='mismatch'?'active':''}" data-f="mismatch"><span class="n">${mm}</span><span class="l">IP Mismatch</span></div>
      <div class="stat off ${catFilter==='offline'?'active':''}" data-f="offline"><span class="n">${off}</span><span class="l">Offline</span></div>
      <div class="stat on ${catFilter==='lan'?'active':''}" data-f="lan"><span class="n">${onlan}</span><span class="l">Seen on LAN</span></div>`;
  }else{
    const list = STATE.devices.length?STATE.devices:STATE.snapshot;
    const on=list.filter(d=>d.ip).length, subs=list.filter(d=>d.sub).length;
    s.innerHTML=`
      <div class="stat ${catFilter==='all'?'active':''}" data-f="all"><span class="n">${list.length}</span><span class="l">Total</span></div>
      <div class="stat on ${catFilter==='lan'?'active':''}" data-f="lan"><span class="n">${on}</span><span class="l">Found on LAN</span></div>
      <div class="stat ${catFilter==='noip'?'active':''}" data-f="noip"><span class="n">${list.length-on}</span><span class="l">No IP</span></div>
      <div class="stat ${catFilter==='sub'?'active':''}" data-f="sub"><span class="n">${subs}</span><span class="l">Sub-devices</span></div>`;
  }
  s.querySelectorAll('.stat').forEach(c=>c.onclick=()=>{ const f=c.dataset.f;
    catFilter=(catFilter===f&&f!=='all')?'all':f; render(); });
}

function renderHead(){
  const h=el('thead');
  if(view==='mismatches'){ h.className='thead grid-mm';
    h.innerHTML=`<div class="th" data-s="title">Device</div><div class="th" data-s="host">HA Host</div>
      <div class="th" data-s="scan">Scanned</div><div class="th" data-s="state">State</div>
      <div class="th" data-s="action" style="justify-content:flex-end">Action</div>`;
  }else{ h.className='thead grid-dev';
    h.innerHTML=`<div class="th" data-s="name">Device</div><div class="th" data-s="ip">IP</div>
      <div class="th" data-s="key">Local key</div><div class="th" data-s="ver" style="justify-content:flex-end">Ver</div>`;
  }
  h.querySelectorAll('.th[data-s]').forEach(th=>th.onclick=()=>{ const k=th.dataset.s;
    if(sortKey===k){ if(sortDir===1)sortDir=-1; else{sortKey=null;sortDir=1;} } else {sortKey=k;sortDir=1;}
    h.querySelectorAll('.th').forEach(x=>x.classList.remove('active'));
    if(sortKey)th.classList.add('active'); render(); });
}

function render(){
  el('titleCount').textContent = STATE.ha_entries.length? `// ${STATE.ha_entries.length} devices`:'';
  el('appVersion').textContent = STATE.version? `· v${STATE.version}`:'';
  el('timestamps').textContent = `sync ${fmtTime(STATE.last_sync)} · scan ${fmtTime(STATE.last_scan)}`;
  renderStats(); renderHead();
  const q=el('search').value.trim().toLowerCase();
  const rows=el('rows'); rows.innerHTML='';
  if(view==='mismatches'){
    let list=STATE.mismatches.slice();
    if(catFilter==='mismatch')list=list.filter(m=>m.mismatch);
    else if(catFilter==='offline')list=list.filter(m=>m.state==='setup_retry');
    else if(catFilter==='lan')list=list.filter(m=>m.found_on_lan);
    if(q)list=list.filter(m=>m.title.toLowerCase().includes(q)||(m.configured_host||'').includes(q));
    if(sortKey)list.sort((a,b)=>{ let av,bv;
      if(sortKey==='host'){av=ipNum(a.configured_host);bv=ipNum(b.configured_host);return (av-bv)*sortDir;}
      if(sortKey==='scan'){av=ipNum(a.scanned_ip);bv=ipNum(b.scanned_ip);return (av-bv)*sortDir;}
      if(sortKey==='state'){av=a.state||'';bv=b.state||'';return av<bv?-sortDir:av>bv?sortDir:0;}
      if(sortKey==='action'){const rank=m=>m.mismatch?0:(m.found_on_lan?1:2);return (rank(a)-rank(b))*sortDir;}
      av=(a.title||'').toLowerCase();bv=(b.title||'').toLowerCase();return av<bv?-sortDir:av>bv?sortDir:0; });
    else list.sort((a,b)=>(b.mismatch-a.mismatch)|| a.title.localeCompare(b.title));
    list.forEach(m=>{
      const row=document.createElement('div'); row.className='row grid-mm';
      const stBadge=m.state==='setup_retry'?'<span class="badge retry">offline</span>':'<span class="badge loaded">loaded</span>';
      let action;
      if(m.mismatch) action=`<button class="fixbtn">Fix</button>`;
      else if(!m.found_on_lan) action=`<span class="nochange">not on LAN</span>`;
      else action=`<span class="ok">✓ match</span>`;
      const scanCell = m.scanned_ip
        ? (m.mismatch?`<span class="new" style="font-family:var(--mono);color:var(--green);font-weight:700">${esc(m.scanned_ip)}</span>`
                     :`<span class="ver">${esc(m.scanned_ip)}</span>`)
        : `<span class="empty">—</span>`;
      row.innerHTML=`<div class="name"><div><div>${esc(m.title)}</div><span class="id">${esc(m.device_id||'')}</span></div></div>
        <div class="copyable" data-copy="${esc(m.configured_host)}"><span class="txt ${m.mismatch?'old':''}" ${m.mismatch?'style=text-decoration:line-through;color:var(--ink-faint)':''}>${esc(m.configured_host||'—')}</span></div>
        <div>${scanCell}</div><div>${stBadge}</div><div style="text-align:right">${action}</div>`;
      const fb=row.querySelector('.fixbtn'); if(fb) fb.onclick=()=>fixOne(m,fb);
      row.querySelectorAll('.copyable[data-copy]').forEach(n=>n.onclick=()=>copy(n.dataset.copy,n));
      rows.appendChild(row);
    });
    if(!list.length) rows.innerHTML=`<div class="row"><div class="nochange" style="padding:20px">No devices — run Refresh HA and Scan LAN.</div></div>`;
  }else{
    let list=(STATE.devices.length?STATE.devices:STATE.snapshot).slice();
    if(catFilter==='lan')list=list.filter(d=>d.ip);
    else if(catFilter==='noip')list=list.filter(d=>!d.ip);
    else if(catFilter==='sub')list=list.filter(d=>d.sub);
    if(q)list=list.filter(d=>d.name.toLowerCase().includes(q)||(d.ip||'').includes(q)||(d.id||'').includes(q));
    list.sort((a,b)=>(a.ip==='')-(b.ip==='')||a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
    if(sortKey)list.sort((a,b)=>{ if(sortKey==='ip')return (ipNum(a.ip)-ipNum(b.ip))*sortDir;
      if(sortKey==='ver')return ((parseFloat(a.ver)||0)-(parseFloat(b.ver)||0))*sortDir;
      const av=(a[sortKey]||'').toLowerCase(),bv=(b[sortKey]||'').toLowerCase();
      if(!av)return 1; if(!bv)return -1; return av<bv?-sortDir:av>bv?sortDir:0; });
    list.forEach(d=>{
      const row=document.createElement('div'); row.className='row grid-dev';
      const badge=d.sub?'<span class="badge sub">sub</span>':(d.ip?'<span class="badge lan">lan</span>':'');
      const ipCell=d.ip?`<div class="copyable" data-copy="${esc(d.ip)}"><span class="txt">${esc(d.ip)}</span></div>`:`<span class="empty">— not on LAN</span>`;
      const keyCell=d.key?`<div class="copyable key" data-copy="${esc(d.key)}"><span>🔒</span><span class="txt">${esc(d.key)}</span></div>`:`<span class="empty">—</span>`;
      row.innerHTML=`<div class="name">${badge}<div><div>${esc(d.name)}</div><span class="id">${esc(d.id||'')}</span></div></div>
        ${ipCell}${keyCell}<div class="ver" style="text-align:right">${esc(d.ver||'·')}</div>`;
      row.querySelectorAll('.copyable[data-copy]').forEach(n=>n.onclick=()=>copy(n.dataset.copy,n));
      rows.appendChild(row);
    });
    if(!list.length) rows.innerHTML=`<div class="row"><div class="nochange" style="padding:20px">No devices — run Sync from Cloud or Scan LAN.</div></div>`;
  }
}

loadState();
