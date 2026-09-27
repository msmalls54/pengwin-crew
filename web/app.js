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
  reserved:'Reserved',submitted:'Submitted',simulated:'Simulated',contained:'Contained',failed:'Failed',held:'Held for review',
  completed:'Completed',pending:'Pending',in_progress:'In progress'
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
  const activityConnection=$('activity-connection');
  if(activityConnection) activityConnection.textContent=`● Read only · read at ${readableTime(data.as_of)}`;
  const proof=data.historical_containment;
  const proofBox=$('judge-containment');
  if(proofBox && proof?.status==='contained'){
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
  } else if(proofBox) {
    proofBox.classList.add('empty-view');
    proofBox.replaceChildren(node('p','','No verified containment receipt has been recorded yet.'));
  }
  if(!activityConnection) return;
  const renderEvents=(target,events,emptyText)=>{
    target.classList.toggle('empty-view',events.length===0);
    target.replaceChildren(...(events.length ? events.map(event=>{
    const item=node('article',`judge-event ${event.status}`);
    const top=node('div','judge-event-top');
    const when=event.recorded_at||event.ts;
    top.append(node('strong','',event.role));
    if(when){const timestamp=node('time','',readableTime(when)); timestamp.dateTime=when;top.append(timestamp);}
    const line=node('div','judge-event-title');
    line.append(node('span','',event.title),node('span',`proof-status ${event.status}`,statusWords[event.status]||'Recorded'));
    item.append(top,line,node('p','judge-proof',event.evidence));
    return item;
    }) : [node('p','',emptyText)]));
  };

  const featured=data.featured_project;
  const memoryBox=$('project-memory');
  if(featured){
    memoryBox.classList.remove('empty-view');
    const head=node('div','project-memory-head');
    head.append(node('div','',featured.title),node('span','proof-status complete','Saved in project memory'));
    const details=node('p','project-memory-meta',`Saved ${readableTime(featured.saved_at)} · latest project update ${readableTime(featured.updated_at)} · ${featured.revision_count} linked ${featured.revision_count===1?'run':'runs'}`);
    const facts=node('div','project-facts');
    const event=featured.event||{};
    const dates=event.date_options?.length ? event.date_options.map(value=>readableTime(`${value}T12:00:00Z`).split(',')[0]).join(' / ') : 'Dates to confirm';
    const venue=node('article','project-fact');
    venue.append(node('span','fact-label','Event'),node('strong','',`Salesforce Park · ${dates}`),node('p','','Venue availability still needs confirmation.'));
    if(event.inquiry_url==='https://www.tjpa.org/permits-reservations'){
      const link=node('a','source-link','Official permit inquiry ↗');
      link.href=event.inquiry_url; link.target='_blank'; link.rel='noopener noreferrer'; venue.append(link);
    }
    const invites=node('article','project-fact');
    invites.append(node('span','fact-label','Invitations'),node('strong','',event.invitation_status==='draft_only'?'Copy drafted':'Copy pending'),
      node('p','','Confirm audience, capacity, and RSVP details before sending.'));
    const buyer=featured.buyer||{};
    const bottles=node('article','project-fact');
    bottles.append(node('span','fact-label','Supplies'),node('strong','',buyer.quantity?`${buyer.quantity} water bottles`:'Quantity pending'),
      node('p','','Choose exact product, artwork, and destination for a checkout quote.'));
    const treasury=featured.treasury||{};
    const budget=node('article','project-fact');
    budget.append(node('span','fact-label','Budget review'),node('strong','',treasury.review_status==='estimate_only'?'Product estimate reviewed':'Review pending'),
      node('p','','Final shipping, tax, and checkout total need review.'));
    facts.append(venue,invites,bottles,budget);
    memoryBox.replaceChildren(head,details,facts,node('p','memory-boundary','This is the latest saved revision. The private Slack conversation and recipient list are not published here.'));
    renderEvents($('judge-timeline'),featured.steps||[],'No completed work is saved for this project yet.');
  } else {
    memoryBox.classList.add('empty-view');
    memoryBox.replaceChildren(node('p','','No public project snapshot is configured.'));
    renderEvents($('judge-timeline'),[],'No featured project steps are available.');
  }
  const decision=$('judge-spend');
  decision.classList.toggle('empty-view',!featured?.buyer);
  const savedBuyer=featured?.buyer;
  if(savedBuyer?.subtotal_min_cents!==null && savedBuyer?.subtotal_min_cents!==undefined &&
     savedBuyer?.subtotal_max_cents!==null && savedBuyer?.subtotal_max_cents!==undefined){
    const amount=node('div','budget-decision-amount',`${money(savedBuyer.subtotal_min_cents,'USD')}–${money(savedBuyer.subtotal_max_cents,'USD')}`);
    const label=node('p','budget-decision-label',`Product estimate for ${savedBuyer.quantity} bottles, checked ${readableTime(savedBuyer.checked_at)}.`);
    const next=node('p','budget-decision-next','Next: get the exact variant, artwork, shipping, tax, and landed checkout total for approval.');
    const status=node('p','budget-decision-status',`Treasurer: ${featured.treasury?.review_status==='estimate_only'?'estimate reviewed':'review pending'} · ${savedBuyer.checkout_status==='not_ready'?'exact delivered quote needed for approval':'checkout status needs review'}.`);
    decision.replaceChildren(amount,label,next,status);
  } else {
    decision.replaceChildren(node('p','','A project-specific estimate is not available yet.'));
  }

}

async function refreshJudge() {
  try {
    const response=await fetch('/api/public-activity');
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    renderJudge(await response.json());
  } catch(error) {
    const status=$('activity-connection')||$('judge-containment');
    if(status) status.textContent=`Activity unavailable · ${error.message}`;
  }
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
  const contained=attempts.some(attempt=>attempt.exit_code===124 &&
    String(attempt.stderr||'').trim()==='Execution timed out after 10 seconds');
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
    $('controls-title').textContent=accessRole==='demo'?'Interactive test unlocked':'Operator controls unlocked';
    $('controls-desc').textContent=accessRole==='demo'
      ? 'Run a new code task or inspect the saved timeout trace. The Slack agent activity record remains read only.'
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
if($('connect')) {
  $('connect').addEventListener('click',connect);
  $('run-code').addEventListener('click',runCode);
  $('freeze').addEventListener('click',()=>action('/api/freeze',{frozen:!latestState?.freeze}));
  $('reset').addEventListener('click',()=>action('/api/reset'));
  setInterval(refresh,5000);
  setInterval(pollCodeRun,2000);
}
setInterval(refreshJudge,15000);
refreshJudge();
