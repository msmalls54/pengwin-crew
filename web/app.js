let adminToken = '';
let accessRole = '';
let latestState = null;
let activeCodeRun = '';
const $ = id => document.getElementById(id);
const money = (cents, currency) => new Intl.NumberFormat('en-US', {style:'currency',currency}).format(cents / 100);
const authHeaders = () => ({'Authorization':`Bearer ${adminToken}`});

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function render(state) {
  latestState = state;
  $('mode').textContent = `${state.modes.model || state.modes.planner} planner · ${state.modes.sandbox} sandbox · ${state.modes.payment} payment${state.freeze ? ' · FROZEN' : ''}`;
  $('freeze').textContent = state.freeze ? 'Unfreeze payments' : 'Freeze payments';
  $('timeline').replaceChildren(...state.events.map(event => {
    const box = node('div', `event ${event.severity}`);
    const row = node('div','row'); row.append(node('strong','',event.agent), node('time','',new Date(event.ts).toLocaleTimeString()));
    box.append(row,node('div','action',event.action.replaceAll('_',' ')),node('div','detail',JSON.stringify(event.detail)));
    return box;
  }));
  $('budgets').replaceChildren(...state.budgets.map(budget => {
    const box = node('div','budget'); const label=node('div','label');
    label.append(node('strong','',`${budget.office} · ${budget.category}`),node('small','',budget.currency));
    const bar=node('div','bar'); const fill=node('span');
    fill.style.width=`${Math.min(100,100*(budget.spent_cents+budget.reserved_cents)/budget.limit_cents)}%`;
    bar.append(fill);
    box.append(label,bar,node('div','numbers',`${money(budget.spent_cents,budget.currency)} used · ${money(budget.reserved_cents,budget.currency)} reserved / ${money(budget.limit_cents,budget.currency)}`));
    return box;
  }));
  const head=node('div','permission head');
  ['Agent','Slack','Quote','Browse','Pay'].forEach(value=>head.append(node('span','',value)));
  $('permissions').replaceChildren(head,...state.permissions.map(agent=>{
    const row=node('div','permission');
    row.append(node('span','',agent.agent));
    ['slack','quote','browse','pay'].forEach(key=>row.append(node('span',agent[key]?'yes':'no',agent[key]===true?'✓':agent[key]===false?'—':agent[key])));
    return row;
  }));
  $('payments').replaceChildren(...state.payments.map(payment=>{
    const button=node('button','payment');
    const left=node('span');left.append(node('strong','',money(payment.amount_cents,payment.currency)),node('small','',payment.id.slice(0,8)));
    const cls=payment.status==='BLOCKED'?'blocked':payment.simulated?'simulated':'submitted';
    button.append(left,node('span',`status ${cls}`,payment.status));
    button.addEventListener('click',()=>showReceipt(payment.id));
    return button;
  }));
}

async function refresh() {
  if(!adminToken||accessRole!=='admin') return;
  try { const response=await fetch('/api/state',{headers:authHeaders()}); if(!response.ok) throw new Error(`HTTP ${response.status}`); render(await response.json()); $('connection').textContent='● Updating'; }
  catch(error) { $('feedback').textContent=`Could not load state: ${error.message}`; }
}

async function action(path, body) {
  adminToken=$('token').value.trim();
  if (!adminToken) { $('feedback').textContent='Enter the demo admin token first.'; return; }
  $('feedback').textContent='Running…';
  try {
    const response=await fetch(path,{method:'POST',headers:{...authHeaders(),'Content-Type':'application/json'},body:JSON.stringify(body??{})});
    const result=await response.json();
    if(!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    const feedback=$('feedback');
    feedback.textContent=result.run_id?`Queued run ${result.run_id}. Watch the timeline and receipts.`:'Updated.';
    const invite=result.results?.find(item=>item.invite_ready);
    if(invite){
      feedback.append(' ');
      const link=node('a','invite-link','Download Berlin lunch invite');
      link.href=`/api/invites/${encodeURIComponent(invite.request_id)}`;
      feedback.append(link);
    }
    await refresh();
  } catch(error) { $('feedback').textContent=`Action failed: ${error.message}`; }
}

async function showReceipt(id) {
  const response=await fetch(`/api/receipts/${encodeURIComponent(id)}`,{headers:authHeaders()});
  if(!response.ok) return;
  const data=await response.json();
  const body=$('receipt-body'); body.replaceChildren(node('h2','',`Payment ${id.slice(0,8)}`));
  const dl=node('dl');
  const fields=[['Status',data.status],['Rule',data.blocked_rule||'Passed'],['Office',data.office],['Vendor',data.vendor],['SKU',data.sku],['Requested',data.requested_qty],['Ordered',data.ordered_qty],['Amount',money(data.amount_cents,data.currency)],['Provider ref',data.provider_ref||'None'],['Mode',data.simulated?'Local simulation':'Airwallex sandbox'],['Order ID',data.pending_order_id],['Error',data.provider_error||'None']];
  fields.forEach(([name,value])=>{dl.append(node('dt','',name),node('dd','',value));});
  body.append(dl); $('receipt').showModal();
}

function renderCodeRun(run) {
  $('code-status').textContent=`Run ${run.id}: ${run.status}. ${run.status==='RUNNING'?'The crew is working.':'The recorded code and output are below.'}`;
  const attempts=run.jobs.flatMap(job=>job.output?.attempts||[]);
  $('code-attempts').replaceChildren(...attempts.map((attempt,index)=>{
    const card=node('div','code-attempt');
    card.append(node('h3','',`Attempt ${index+1} · exit ${attempt.exit_code} · ${attempt.code_hash}`));
    card.append(node('div','code-label','Executed Python'),node('pre','',attempt.code));
    card.append(node('div','code-label','stdout'),node('pre','',attempt.stdout||'(empty)'));
    if(attempt.stderr) card.append(node('div','code-label','stderr'),node('pre error-output',attempt.stderr));
    return card;
  }));
  if(['COMPLETE','FAILED','HELD'].includes(run.status)) activeCodeRun='';
}

async function pollCodeRun() {
  if(!activeCodeRun||!adminToken) return;
  try {
    const response=await fetch(`/api/code-runs/${encodeURIComponent(activeCodeRun)}`,{headers:authHeaders()});
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    renderCodeRun(await response.json());
  } catch(error) { $('code-status').textContent=`Could not read run: ${error.message}`; }
}

async function runCode() {
  adminToken=$('token').value.trim();
  const goal=$('code-goal').value.trim();
  if(!adminToken) { $('code-status').textContent='Enter the demo token first.'; return; }
  if(!goal) { $('code-status').textContent='Describe a calculation or data task first.'; return; }
  $('code-status').textContent='Queueing the task…';
  $('code-attempts').replaceChildren();
  try {
    const response=await fetch('/api/code-runs',{method:'POST',headers:{...authHeaders(),'Content-Type':'application/json'},body:JSON.stringify({goal})});
    const data=await response.json();
    if(!response.ok) throw new Error(data.detail||`HTTP ${response.status}`);
    activeCodeRun=data.run_id;
    await pollCodeRun();
  } catch(error) { $('code-status').textContent=`Could not queue task: ${error.message}`; }
}

async function connect() {
  adminToken=$('token').value.trim();
  if(!adminToken) { $('feedback').textContent='Enter an access token first.'; return; }
  try {
    const response=await fetch('/api/demo-access',{headers:authHeaders()});
    if(!response.ok) throw new Error('The token was not accepted.');
    accessRole=(await response.json()).access;
    document.body.classList.toggle('demo-only',accessRole==='demo');
    $('mode').textContent=accessRole==='demo'?'Sandbox demo connected':'Operator connected';
    $('controls-title').textContent=accessRole==='demo'?'Sandbox demo ready':'Run a request';
    $('controls-desc').textContent=accessRole==='demo'
      ? 'Give the crew a plain-English code task below. Generated code and real output appear in this browser.'
      : 'Each flow leaves an audit trail. The hoodie page contains a malicious quantity instruction; compare the Buyer’s proposal with the payment decision.';
    $('feedback').textContent='Connected.';
    $('code-status').textContent='Describe a calculation or small data task to run in the sandbox.';
    await refresh();
  } catch(error) { accessRole=''; $('feedback').textContent=error.message; }
}

document.querySelectorAll('[data-flow]').forEach(button=>button.addEventListener('click',()=>action(`/api/demo/${button.dataset.flow}`)));
$('connect').addEventListener('click',connect);
$('run-code').addEventListener('click',runCode);
$('freeze').addEventListener('click',()=>action('/api/freeze',{frozen:!latestState?.freeze}));
$('reset').addEventListener('click',()=>action('/api/reset'));
setInterval(refresh,5000);
setInterval(pollCodeRun,2000);
