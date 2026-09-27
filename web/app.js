let adminToken = '';
let accessRole = '';
let latestState = null;
let activeCodeRun = '';
const $ = id => document.getElementById(id);
const money = (cents, currency) => new Intl.NumberFormat('en-US', {style:'currency',currency}).format(cents / 100);
const authHeaders = () => ({'Authorization':`Bearer ${adminToken}`});
const readableTime = value => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Time unavailable' : new Intl.DateTimeFormat('en-US', {
    month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'
  }).format(date);
};
const statusWords = {
  QUEUED:'Requested',RUNNING:'In progress',WAITING_APPROVAL:'Awaiting human approval',
  COMPLETE:'Completed',DONE:'Completed',FAILED:'Failed',HELD:'Held for review',REJECTED:'Rejected',
  queued:'Requested',complete:'Recorded',proposed:'Proposed',approved:'Approved',blocked:'Blocked',
  reserved:'Reserved',submitted:'Submitted',simulated:'Simulated',contained:'Contained',failed:'Failed',held:'Held for review'
};

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

function renderJudge(data) {
  $('activity-connection').textContent=`● Read only · updated ${readableTime(data.as_of)}`;
  const proof=data.historical_containment;
  const proofBox=$('judge-containment');
  if(proof?.status==='contained'){
    proofBox.classList.remove('empty-view');
    const lead=node('div','historical-proof-head');
    lead.append(node('strong','','Ten-second container limit enforced'),node('span','proof-status contained','Contained'));
    const recorded=node('time','',`Recorded ${readableTime(proof.recorded_at)} · saved result, not a current run`);
    recorded.dateTime=proof.recorded_at;
    const inspect=node('button','historical-load','Inspect full trace with demo token');
    inspect.type='button';
    inspect.addEventListener('click',()=>loadHistoricalCodeRun(proof.run_id));
    proofBox.replaceChildren(lead,recorded,node('p','judge-proof',proof.evidence),
      node('p','historical-source',`${proof.source} · run ${proof.run_id} · audit event ${proof.audit_event_id}`),inspect);
  } else {
    proofBox.classList.add('empty-view');
    proofBox.replaceChildren(node('p','','No verified containment receipt has been recorded yet.'));
  }
  const events=data.activity||[];
  $('judge-timeline').classList.toggle('empty-view',events.length===0);
  $('judge-timeline').replaceChildren(...(events.length ? events.map(event=>{
    const item=node('article',`judge-event ${event.status}`);
    const top=node('div','judge-event-top');
    const timestamp=node('time','',readableTime(event.ts)); timestamp.dateTime=event.ts;
    top.append(node('strong','',event.role),timestamp);
    const line=node('div','judge-event-title');
    line.append(node('span','',event.title),node('span',`proof-status ${event.status}`,statusWords[event.status]||'Recorded'));
    item.append(top,line,node('p','judge-proof',event.evidence));
    return item;
  }) : [node('p','','No recorded activity is available yet.')]));

  const runs=data.runs||[];
  $('judge-runs').classList.toggle('empty-view',runs.length===0);
  $('judge-runs').replaceChildren(...(runs.length ? runs.map(run=>{
    const item=node('article','judge-run');
    const head=node('div','judge-run-head');
    head.append(node('strong','',run.flow),node('span',`proof-status ${run.status.toLowerCase()}`,statusWords[run.status]||'Recorded'));
    item.append(head,node('time','',readableTime(run.created_at)));
    const steps=node('div','judge-steps');
    (run.steps||[]).forEach(step=>steps.append(node('span','judge-step',`${step.role}: ${step.task} · ${statusWords[step.status]||'Recorded'}`)));
    item.append(steps);
    return item;
  }) : [node('p','','No recent tasks are recorded.')]));

  const groups=[
    ['Proposed mock orders',data.spend?.proposed_mock_orders||[],'Quoted from fictional stores. This is not spending.'],
    ['Simulated checkouts',data.spend?.simulated_checkouts||[],'Recorded locally; no real charge.'],
    ['Sandbox transfers submitted',data.spend?.submitted_sandbox_transfers||[],'Submission is recorded; settlement is unconfirmed.']
  ];
  $('judge-spend').classList.remove('empty-view');
  const spendItems=groups.map(([label,amounts,description])=>{
    const item=node('div','spend-row');
    const values=amounts.length ? amounts.map(entry=>`${money(entry.amount_cents,entry.currency)} (${entry.count})`).join(' · ') : 'None recorded';
    item.append(node('strong','',label),node('span','spend-value',values),node('small','',description));
    return item;
  });
  const estimate=data.spend?.water_bottle_product_estimate;
  const estimateItem=node('div','spend-row estimate-row');
  estimateItem.append(node('strong','','Sourced water-bottle range'));
  if(estimate){
    estimateItem.append(node('span','spend-value',`${money(estimate.unit_min_cents,'USD')}–${money(estimate.unit_max_cents,'USD')} each`));
    if(estimate.quantity!==null){
      estimateItem.append(node('small','',`${estimate.quantity} bottles · estimated product subtotal ${money(estimate.subtotal_min_cents,'USD')}–${money(estimate.subtotal_max_cents,'USD')}`));
    }
    estimateItem.append(node('small','',`Official catalog range checked ${readableTime(estimate.checked_at)}. Shipping, tax, design, stock, and final checkout total are unverified.`));
    if(estimate.source_url==='https://www.printful.com/custom-water-bottles'){
      const link=node('a','source-link','View Printful catalog source ↗');
      link.href=estimate.source_url; link.target='_blank'; link.rel='noopener noreferrer';
      estimateItem.append(link);
    }
  } else {
    estimateItem.append(node('span','spend-value','No verified range recorded'),
      node('small','','A search result alone is not a product price or checkout quote.'));
  }
  spendItems.splice(1,0,estimateItem);
  const realItem=node('div','spend-row real-spend');
  realItem.append(node('strong','','Real settled spend'),node('span','spend-value','Not verified here'),
    node('small','','Pengwin has no production settlement ledger. These figures do not represent a bank balance.'));
  spendItems.push(realItem);
  $('judge-spend').replaceChildren(...spendItems);

  const usage=data.model_usage||{};
  $('judge-model').classList.remove('empty-view');
  $('judge-model').replaceChildren(node('div','model-count',`${usage.attempted_calls||0} / ${usage.call_limit||0}`),
    node('p','',`Attempted ${usage.model||'model'} calls against the configured limit. This count includes requests that later failed.`),
    node('p','model-cost','Billed token usage and model cost are not recorded by this app.'));
}

async function refreshJudge() {
  try {
    const response=await fetch('/api/public-activity');
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    renderJudge(await response.json());
  } catch(error) { $('activity-connection').textContent=`Activity unavailable · ${error.message}`; }
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
  const attempts=run.jobs.flatMap(job=>job.output?.attempts||[]);
  const contained=attempts.some(attempt=>attempt.exit_code===124);
  $('code-status').textContent=`Run ${run.id}: ${run.status}. ${run.status==='RUNNING'?'The crew is working.':contained?'The ten-second timeout was contained; inspect the trace below.':'The recorded code and output are below.'}`;
  $('code-attempts').replaceChildren(...attempts.map((attempt,index)=>{
    const card=node('div','code-attempt');
    card.append(node('h3','',`Attempt ${index+1} · exit ${attempt.exit_code} · ${attempt.code_hash}`));
    card.append(node('div','code-label','Executed Python'),node('pre','',attempt.code));
    card.append(node('div','code-label','stdout'),node('pre','',attempt.stdout||'(empty)'));
    if(attempt.stderr) card.append(node('div','code-label','stderr'),node('pre','error-output',attempt.stderr));
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

async function loadHistoricalCodeRun(runId) {
  adminToken=$('token').value.trim();
  if(!adminToken){
    $('code-status').textContent='Enter the private demo token to inspect this recorded run.';
    $('token').focus();
    return;
  }
  activeCodeRun=runId;
  await pollCodeRun();
  $('code-status').scrollIntoView({behavior:'smooth',block:'center'});
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
    await Promise.all([refresh(),refreshJudge()]);
  } catch(error) { accessRole=''; $('feedback').textContent=error.message; }
}

document.querySelectorAll('[data-flow]').forEach(button=>button.addEventListener('click',()=>action(`/api/demo/${button.dataset.flow}`)));
document.querySelectorAll('[data-code-goal]').forEach(button=>button.addEventListener('click',()=>{
  $('code-goal').value=button.dataset.codeGoal;
  $('code-goal').focus();
}));
$('connect').addEventListener('click',connect);
$('run-code').addEventListener('click',runCode);
$('freeze').addEventListener('click',()=>action('/api/freeze',{frozen:!latestState?.freeze}));
$('reset').addEventListener('click',()=>action('/api/reset'));
setInterval(refresh,5000);
setInterval(refreshJudge,15000);
setInterval(pollCodeRun,2000);
refreshJudge();
