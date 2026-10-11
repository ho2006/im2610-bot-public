'use strict';
const $ = id => document.getElementById(id);
let token = '', state = null, showingTrades = false, configBuilt = false, busy = false;
const fmt = (v, digits=1) => Number.isFinite(v) ? v.toLocaleString('zh-CN', {minimumFractionDigits:digits, maximumFractionDigits:digits}) : '—';
const stamp = ts => ts ? new Date(ts*1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false}) : '—';
const operations = {'0':'买开', '1':'卖开', '2':'买平', '3':'卖平'};
const states = {PREPARED:'意图已落库',UNKNOWN:'状态不明',ACK:'已受理待对账',OPEN:'未成交',PARTIAL:'部分成交',FILLED:'全部成交',CANCELLING:'撤单待确认',UNKNOWN_CANCEL:'撤单状态不明',CANCELLED:'已撤单',REJECTED:'已拒绝',NOT_SENT:'未发送'};
const labels = {poll_seconds:'轮询间隔 / 秒',max_quote_age:'最大行情年龄 / 秒',order_lifetime:'委托存活 / 秒',reprice_ticks:'改价阈值 / ticks',order_size:'每笔手数',max_position:'最大总持仓 / 手',max_daily_loss:'每日亏损阈值 / 元',max_use_rate:'最高保证金使用率 / 0–1',margin_rate:'估算保证金比例 / 0–1',half_spread_ticks:'基础半价差 / ticks',edge_ticks:'额外报价优势 / ticks',fee_per_side_bps:'单边手续费 / bps（模拟盘 0）',inventory_skew_ticks:'每手库存偏移 / ticks',imbalance_cutoff:'失衡单侧过滤 / 0–1',ema_alpha:'Microprice 平滑系数',max_spread_ticks:'最大盘口价差 / ticks',max_orders_per_day:'每日开仓委托上限',flatten_time:'收盘前开始减仓',entry_filter:'开仓环境过滤（推荐开启）'};
function toast(text) {$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,5500);}
async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'},signal:AbortSignal.timeout(12000)});
  const body=await response.json();
  if (!response.ok) throw new Error(body.error || `请求失败 ${response.status}`);
  return body;
}
function set(id,text){$(id).textContent=text;}
function table() {
  const rows=showingTrades ? state.trades : state.orders;
  const headings=showingTrades ? ['成交时间','合约','方向','成交价','手数'] : ['提交时间','合约','方向','限价','手数 / 剩余','状态'];
  $('table-head').replaceChildren();const head=document.createElement('tr');
  headings.forEach(t=>{const th=document.createElement('th');th.textContent=t;head.append(th);});$('table-head').append(head);
  const body=$('table-body');body.replaceChildren();
  if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=headings.length;td.className='empty';td.textContent=showingTrades?'今日暂无成交':'暂无委托。启动策略后将在此显示持久化记录。';tr.append(td);body.append(tr);return;}
  rows.slice(0,60).forEach(row=>{
    const tr=document.createElement('tr');
    const values=showingTrades ? [row.time,'IM2610',row.operation,fmt(row.price),row.qty] : [stamp(row.created),'IM2610',operations[row.op],fmt(row.price),`${row.qty} / ${row.remaining}`,states[row.state]||'未知状态'];
    values.forEach((v,i)=>{const td=document.createElement('td');td.textContent=String(v);if(i===2)td.className=String(v).startsWith('买')?'positive':'negative';if(i===5)td.className='state-pill';tr.append(td);});body.append(tr);
  });
}
function configForm(config) {
  if(configBuilt)return;
  const form=$('config-form');form.replaceChildren();
  Object.entries(config).forEach(([key,value])=>{const label=document.createElement('label');label.textContent=labels[key]||key;const input=document.createElement('input');input.name=key;if(typeof value==='boolean'){input.type='checkbox';input.checked=value;input.className='toggle';}else{input.type=key==='flatten_time'?'time':'number';input.step='any';input.value=value;input.required=true;}label.append(input);form.append(label);});
  configBuilt=true;
}
function chart() {
  if(!state)return;
  const canvas=$('price-chart'), dpr=window.devicePixelRatio||1, w=canvas.clientWidth,h=canvas.clientHeight;
  canvas.width=w*dpr;canvas.height=h*dpr;const c=canvas.getContext('2d');c.scale(dpr,dpr);c.clearRect(0,0,w,h);
  const data=state.ticks||[], values=data.flatMap(t=>[t.mid,t.microprice,t.fair]).filter(Number.isFinite);
  if(!values.length){c.fillStyle='#6d8999';c.font='12px sans-serif';c.textAlign='center';c.fillText('等待第一个有效盘口快照',w/2,h/2);return;}
  let low=Math.min(...values), high=Math.max(...values);const pad=Math.max((high-low)*.2,.6);low-=pad;high+=pad;
  const left=22,right=w-75,top=18,bottom=h-30;
  const y=v=>bottom-(v-low)/(high-low)*(bottom-top), x=i=>left+(right-left)*(data.length>1?i/(data.length-1):.5);
  c.font='10px monospace';c.lineWidth=1;
  for(let i=0;i<=4;i++){const yy=top+(bottom-top)*i/4;c.strokeStyle='#21303b';c.beginPath();c.moveTo(left,yy);c.lineTo(right,yy);c.stroke();c.fillStyle='#6b8493';c.fillText((high-(high-low)*i/4).toFixed(1),right+12,yy+3);}
  [['mid','#78a9ef'],['microprice','#7ee2bf'],['fair','#e1c985']].forEach(([key,color])=>{c.strokeStyle=color;c.fillStyle=color;c.lineWidth=key==='microprice'?1.8:1.1;c.beginPath();let started=false;data.forEach((row,i)=>{if(!Number.isFinite(row[key]))return;if(!started){c.moveTo(x(i),y(row[key]));started=true;}else c.lineTo(x(i),y(row[key]));});c.stroke();const last=data.at(-1);if(Number.isFinite(last[key])){c.beginPath();c.arc(x(data.length-1),y(last[key]),2.5,0,Math.PI*2);c.fill();}});
  const short=t=>new Date(t*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});c.fillStyle='#617c8b';c.fillText(short(data[0].timestamp),left,h-9);c.textAlign='right';c.fillText(short(data.at(-1).timestamp),right,h-9);
}
function render() {
  $('login').hidden=true;$('dashboard').hidden=false;
  set('connection','● 已连接');set('mode',state.mode==='competition'?'比赛模拟账户':'本地纸面撮合');$('mode').className='badge'+(state.mode==='competition'?' live':'');
  set('note',[state.note,...(state.alerts||[])].join(' · '));$('notice').className='notice'+(state.latched||state.alerts?.length?' error':'');
  set('cycle-age',`状态 ${Math.max(0,state.server_time-state.last_cycle).toFixed(0)} 秒前 · 本轮 ${fmt(state.cycle_seconds,1)} 秒`);
  const q=state.quote||{},a=state.account||{},h=state.holdings||{};
  set('last-price',fmt(q.last));set('micro',fmt(q.microprice,3));set('mid',fmt(q.mid));set('quote-time',q.timestamp?stamp(q.timestamp).split(' ').at(-1):'—');
  set('equity',fmt(a.equity,0));set('pnl',fmt(state.daily.pnl,2));$('pnl').className=state.daily.pnl>=0?'positive':'negative';
  set('inventory',fmt(h.long-h.short,0));set('long',h.long??'—');set('short',h.short??'—');set('use-rate',Number.isFinite(a.use_rate)?fmt(a.use_rate*100,1)+'%':'—');
  set('ask',fmt(q.ask));set('bid',fmt(q.bid));set('ask-size',fmt(q.ask_size,0));set('bid-size',fmt(q.bid_size,0));set('book-mid',fmt(q.mid));set('spread',fmt((q.ask-q.bid)/.2,0)+' ticks');
  set('obi',fmt(q.imbalance,3));$('obi-meter').value=q.imbalance||0;
  const age=state.server_time-(q.timestamp||0);set('feed-age',q.timestamp?`${age>state.config.max_quote_age?'过期快照':'行情年龄'} · ${Math.max(0,age).toFixed(0)} 秒`:'行情未就绪');
  set('strategy-state',state.latched?'LOCKED':state.enabled?(state.flatten?'REDUCE ONLY':'RUNNING'):'PAUSED');
  set('plan-text',state.plans?.length?state.plans.map(p=>`${operations[p.op]} ${p.qty}手 @ ${fmt(p.price)}`).join('  /  '):'当前无新增报价');
  const filter=state.entry_filter,filterStale=Number.isFinite(filter?.source_time)&&state.server_time-filter.source_time>state.config.max_quote_age;
  set('entry-filter-state',state.config.entry_filter===false?'开仓环境过滤已关闭':filter?`开仓环境：${filterStale?'已过期':filter.allowed?'环境通过':'暂停开仓'} · ${filter.reason} · ${filter.samples} 个样本 / 覆盖 ${fmt(filter.span_seconds,0)} 秒`:'开仓环境：等待行情');
  document.querySelector('[data-command="start"]').disabled=state.enabled||state.latched||!state.ready;
  document.querySelector('[data-command="flatten"]').disabled=state.latched||!state.ready;
  $('save-config').disabled=state.enabled||state.active_orders>0;
  const events=$('event-list');events.replaceChildren();state.events.forEach(row=>{const line=document.createElement('div');line.className='event '+row.level;const t=document.createElement('time');t.textContent=stamp(row.ts);const text=document.createElement('span');text.textContent=row.message;line.append(t,text);events.append(line);});
  if(!state.events.length){const empty=document.createElement('div');empty.className='event';empty.textContent='暂无运行事件';events.append(empty);}
  configForm(state.config);table();chart();
}
async function refresh(){if(!token||busy)return;busy=true;const requestToken=token;try{const next=await api('/api/state');if(token!==requestToken)return;state=next;render();}catch(error){if(token===requestToken){set('connection','● 连接异常');toast(error.message);}}finally{busy=false;}}
$('login-form').addEventListener('submit',async e=>{e.preventDefault();token=$('token').value.trim();await refresh();if(state)$('token').value='';});
$('logout').addEventListener('click',()=>{token='';state=null;$('login').hidden=false;$('dashboard').hidden=true;set('connection','未连接');toast('控制台已锁定；服务器策略继续按原状态运行');});
document.querySelectorAll('[data-command]').forEach(button=>button.addEventListener('click',async()=>{try{await api('/api/control',{method:'POST',body:JSON.stringify({action:button.dataset.command})});toast('命令已排队，执行结果见运行日志');await refresh();}catch(error){toast(error.message);}}));
$('save-config').addEventListener('click',async()=>{const form=$('config-form');if(!form.reportValidity())return;const config={};for(const [key,val]of new FormData(form))config[key]=key==='flatten_time'?val:Number(val);form.querySelectorAll('input[type="checkbox"]').forEach(input=>config[input.name]=input.checked);try{await api('/api/config',{method:'PUT',body:JSON.stringify(config)});toast('参数保存已排队；运行日志会显示是否成功');}catch(error){toast(error.message);}});
$('orders-tab').addEventListener('click',()=>{showingTrades=false;$('orders-tab').classList.add('selected');$('trades-tab').classList.remove('selected');table();});
$('trades-tab').addEventListener('click',()=>{showingTrades=true;$('trades-tab').classList.add('selected');$('orders-tab').classList.remove('selected');table();});
document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',()=>{document.querySelectorAll('nav a').forEach(n=>n.classList.remove('active'));a.classList.add('active');}));
window.addEventListener('resize',chart);
setInterval(refresh,4000);setInterval(()=>set('clock',new Date().toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+' CST'),1000);
