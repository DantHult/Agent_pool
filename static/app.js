'use strict';

const $ = id => document.getElementById(id);
const terminalStates = new Set(['completed', 'failed', 'cancelled']);
const executionModes = {
  simulation: 'Demo / simulated',
  native: 'NATIVE LOCAL · Codex',
  organization_api: 'Organization API',
  personal_supported: 'Personal subscription · unsupported'
};
let overview = null;
let detail = null;
let project = null;
let events = [];
let historyEvents = [];
let historyAfter = 0;
let tab = 'files';
let selectedFile = null;
let selectedStartWorker = '';
let selectedTarget = '';
let selectedReason = 'manual_switch';
let busy = false;
let loading = false;
let viewVersion = 0;
let lastConversation = null;
let refreshSerial = 0;
let authMode = 'invite';
let csrfToken = '';
let registration = false;
let liveSource = null;
let liveTimer = null;
let nativeProfiles = null;
let selectedConversation = null;
let conversations = [];
let projectTasks = [];
let assignmentTarget = '';
let activityFrame = null;
let hasOlderActivity=false, activityLoading=false, workersLoading=false, workerTimer=null, artifactLoading=false;
const activityView = new RelayActivity.View($('timeline'), workerName);

const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[character]));
const displayDate = value => value ? new Date(value).toLocaleString() : 'Not reported';

async function api(path, body) {
  const requestedVersion=viewVersion;
  const response = await fetch('/api/' + path, {
    method: body ? 'POST' : 'GET',
    headers: body ? {'Content-Type': 'application/json', ...(csrfToken ? {'X-Relay-CSRF': csrfToken} : {})} : {},
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await response.json();
  if (!response.ok) {
    const failure = new Error(data.error || 'Request failed');
    failure.status = response.status;
    failure.code = data.code;
    if (response.status === 401 && authMode !== 'demo' && requestedVersion===viewVersion) showSignIn();
    throw failure;
  }
  return data;
}

function showError(failure) {
  $('error').hidden = false;
  $('error').textContent = failure.message;
}

function showNotice(message) {
  $('notice').hidden = !message;
  $('notice').textContent = message || '';
}

function activeTask() {
  return detail?.tasks?.find(task => !terminalStates.has(task.state));
}

function projectWorkers() {
  return detail?.workers || overview?.workers || [];
}

function workerName(id) {
  const worker = [...projectWorkers(),...conversations.flatMap(item=>item.participants || [])].find(item => item.id === id);
  return worker?.display_name || worker?.name || id || 'Unassigned';
}

function ownerName(worker) {
  return worker.owner_name || (worker.owner_type === 'org'
    ? overview?.organizations?.find(org => org.id === worker.owner_id)?.name
    : overview?.users?.find(user => user.id === worker.owner_id)?.name) || worker.owner_id;
}

function canManage(worker) {
  return worker.can_manage === true && currentCanWrite();
}

function eligible(worker) {
  return worker.eligible === true;
}

function clearHistory() {
  historyEvents = [];
  historyAfter = 0;
  $('history').textContent = '';
  $('more').textContent = 'Load next 100 events';
  selectedFile = null;
}

function invalidateView() {
  if (liveSource) liveSource.close();
  liveSource = null;
  if (liveTimer) clearTimeout(liveTimer);
  liveTimer = null;
  viewVersion += 1;
  refreshSerial += 1;
  lastConversation = null;
  events = [];
  detail = null;
  nativeProfiles = null;
  selectedConversation = null;
  conversations = [];
  projectTasks = [];
  hasOlderActivity=false;activityLoading=false;workersLoading=false;artifactLoading=false;clearTimeout(workerTimer);
  activityView.reset(null);
  selectedStartWorker = '';
  selectedTarget = '';
  clearHistory();
  $('timeline').innerHTML = '<div class="empty">Loading authorized project history…</div>';
  $('artifactBody').textContent = '';
  $('handoffs').textContent = '';
  $('workers').textContent = '';
  $('conversationList').textContent='';
  $('projectTasks').textContent='';
  $('taskObjective').textContent='Loading conversation…';$('taskObjectiveFull').textContent='';$('activeWorkerLabel').textContent='No worker selected';
}

function setControlsDisabled(disabled) {
  $('user').disabled = disabled;
  $('project').disabled = disabled;
  $('newProject').disabled = disabled || overview?.user.role === 'viewer';
  $('enroll').disabled = disabled;
  $('inviteMember').disabled = disabled || !project || !detail;
  $('manageMembers').disabled = disabled || !project || !detail;
  $('newConversation').disabled = disabled || !project || !detail || !currentCanWrite();
}

async function login() {
  if (busy) return;
  busy = true;
  loading = false;
  setControlsDisabled(true);
  RelayIDE.signOut();invalidateView();
  $('error').hidden = true;
  showNotice('');
  project = null;
  try {
    await api('login', {user_id: $('user').value});
    await load(true);
  } catch (failure) {
    showError(failure);
  } finally {
    busy = false;
    setControlsDisabled(false);
    if (detail) render();
  }
}

async function load(force = false) {
  if (loading && !force) return;
  const version=viewVersion,serial=++refreshSerial;
  loading=true;if(!detail)setControlsDisabled(true);
  try {
    const nextOverview=await api('overview?light=1');
    if(version!==viewVersion || serial!==refreshSerial)return;
    showWorkspaceIdentity(nextOverview);
    const nextProject=nextOverview.projects.some(item=>item.id===project)?project:nextOverview.projects[0]?.id;
    if(!nextProject){invalidateView();RelayIDE.signOut();overview=nextOverview;project=null;$('project').innerHTML='';$('timeline').innerHTML='<div class="empty">No authorized projects.</div>';return;}
    const [listed,taskList]=await Promise.all([
      api('ide/conversations?'+new URLSearchParams({project_id:nextProject})),
      nextOverview.features?.agent_management?api('tasks?'+new URLSearchParams({project_id:nextProject})):Promise.resolve({tasks:[]})
    ]);
    if(version!==viewVersion || serial!==refreshSerial)return;
    conversations=listed.conversations;projectTasks=taskList.tasks;
    const remembered=sessionStorage.getItem('relay-conversation:'+nextOverview.user.id+':'+nextProject);
    const choice=[selectedConversation,remembered].find(id=>conversations.some(item=>item.id===id)) || conversations.find(item=>!item.archived_at)?.id || conversations[0]?.id;
    const nextDetail=await api('projects/'+encodeURIComponent(nextProject)+'?'+new URLSearchParams({conversation_id:choice || '',light:'1'}));
    if(version!==viewVersion || serial!==refreshSerial)return;
    const conversation=nextDetail.conversation.id,changed=lastConversation!==conversation;
    if(changed){clearHistory();events=[];hasOlderActivity=false;activityLoading=true;activityView.reset(conversation);selectedStartWorker='';selectedTarget='';}
    nextDetail.workers=detail?.conversation.id===conversation?detail.workers:[];nextDetail.workersLoaded=detail?.conversation.id===conversation && detail.workersLoaded;
    if(!changed && detail && !detail.light && detail.workspace.checkpoint===nextDetail.workspace.checkpoint)Object.assign(nextDetail,{light:false,workspace:detail.workspace,contexts:detail.contexts,handoffs:detail.handoffs,execution_runs:detail.execution_runs,assignments:detail.assignments});
    overview=nextOverview;detail=nextDetail;project=nextProject;selectedConversation=conversation;lastConversation=conversation;
    sessionStorage.setItem('relay-conversation:'+overview.user.id+':'+project,conversation);
    const options=overview.projects.map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
    if($('project').innerHTML!==options)$('project').innerHTML=options;
    $('project').value=project;$('user').value=overview.user.id;
    render(); // Files/editor no longer wait for provider probes or event history.
    loadActivity(conversation,version,changed).catch(showError);
    if(changed || !workersLoading) {
      clearTimeout(workerTimer);
      workerTimer=setTimeout(()=>loadWorkers(version,project).catch(showError),changed?150:0);
    }
  } finally {
    if(version===viewVersion && serial===refreshSerial){loading=false;setControlsDisabled(busy);}
  }
}

async function loadActivity(conversation,version,changed) {
  if(activityLoading && !changed)return;
  activityLoading=true;
  try {
    let after=events.at(-1)?.sequence || 0;
    const recent=changed && overview.features?.recent_history;
    let page=await api(`conversations/${encodeURIComponent(conversation)}/events?limit=100&${recent?'recent=1':'after='+after}`);
    while(true) {
      if(version!==viewVersion || lastConversation!==conversation)return;
      if(recent)hasOlderActivity=page.has_older===true;
      const merged=new Map([...events,...page.events].map(event=>[event.event_id,event]));events=[...merged.values()].sort((a,b)=>a.sequence-b.sequence);
      renderConversation();after=events.at(-1)?.sequence || after;subscribe(conversation,after);
      if(recent || page.events.length<100)break;
      await new Promise(resolve=>setTimeout(resolve,0));
      page=await api(`conversations/${encodeURIComponent(conversation)}/events?limit=100&after=${after}`);
    }
  }finally{if(version===viewVersion){activityLoading=false;renderConversation();}}
}

async function loadWorkers(version,selectedProject) {
  if(version!==viewVersion || project!==selectedProject || workersLoading)return;
  // Wait for the editor's lightweight workspace request before optional probes.
  if(!RelayIDE.getState()){workerTimer=setTimeout(()=>loadWorkers(version,selectedProject).catch(showError),100);return;}
  workersLoading=true;
  try {
    const [directory,profiles]=await Promise.all([
      api('workers?'+new URLSearchParams({project_id:selectedProject,cached:'1'})),
      authMode==='local-owner'?api('native/profiles?'+new URLSearchParams({project_id:selectedProject,cached:'1'})):Promise.resolve(null)
    ]);
    if(version!==viewVersion || project!==selectedProject)return;
    detail.workers=directory.workers;detail.workersLoaded=true;overview.workers=directory.workers;nativeProfiles=profiles;render();
    if(directory.workers.some(worker=>worker.health?.status==='checking') || profiles?.profiles.some(profile=>profile.status==='checking'))workerTimer=setTimeout(()=>loadWorkers(version,selectedProject).catch(showError),500);
  }finally{workersLoading=false;}
}

async function loadArtifacts() {
  if(!detail?.light || artifactLoading)return;
  const version=viewVersion,selectedProject=project,conversation=detail.conversation.id;artifactLoading=true;
  try {
    const full=await api('projects/'+encodeURIComponent(project)+'?'+new URLSearchParams({conversation_id:conversation}));
    if(version!==viewVersion || project!==selectedProject || detail.conversation.id!==conversation)return;
    detail={...full,workers:detail.workers};renderArtifact();
  }catch(error){showError(error);}finally{artifactLoading=false;}
}

function renderWorker(worker, task) {
  const active = task?.worker_id === worker.id;
  const capacity = worker.capacity || {};
  const mode = worker.execution_mode || 'simulation';
  const status = worker.status || worker.state || capacity.availability || 'offline';
  const projects = worker.permitted_project_ids || [];
  const modeLabel = executionModes[mode] || mode;
  const scope = mode === 'organization_api' ? 'Shared organization API quota' :
    mode === 'native' ? 'Local ChatGPT profile · owner initiated' :
    mode === 'personal_supported' ? 'Unsupported · no execution' : 'Independent simulated capacity';
  const reason = worker.eligibility_reason || (eligible(worker) ? 'Approved and available for this project' : 'Not eligible for this project');
  const availabilityControl = mode === 'personal_supported'
    ? '<select name="state" disabled><option value="unsupported">Unsupported</option></select>'
    : `<select name="state">${['available', 'paused', 'offline', 'quota_blocked'].map(value => `<option value="${value}" ${status === value || capacity.availability === value ? 'selected' : ''}>${value.replace('_', ' ')}</option>`).join('')}</select>`;
  const settings = canManage(worker) ? `<details class="worker-settings" data-settings="${esc(worker.id)}"><summary>Owner settings</summary><form data-worker-form="${esc(worker.id)}"><label>Display name<input name="display_name" maxlength="100" required value="${esc(worker.display_name || worker.name)}"></label><label>Availability${availabilityControl}</label><label class="checkbox-row"><input type="checkbox" name="opt_in" ${worker.opt_in ? 'checked' : ''}> Opt in to eligible assignments</label><fieldset><legend>Explicit project approvals</legend>${overview.projects.map(item => `<label class="checkbox-row"><input type="checkbox" name="project_ids" value="${esc(item.id)}" ${projects.includes(item.id) ? 'checked' : ''}> ${esc(item.name)}</label>`).join('')}</fieldset><div class="settings-actions"><button class="outline" type="submit">Save settings</button><button class="quiet danger" type="button" data-revoke="${esc(worker.id)}">Opt out & stop safely</button></div></form></details>` : '<div class="permission-note">Settings controlled by the worker owner or administrator</div>';
  return `<article class="worker-entry ${active ? 'active' : ''}"><div class="worker"><span class="worker-icon">⌘</span><div><strong>${esc(worker.display_name || worker.name)}</strong><small>${esc(ownerName(worker))} · ${worker.owner_type === 'org' ? 'Organization owned' : 'User owned'}</small></div><span class="state">${esc(active ? task.state.replaceAll('_', ' ') : status.replaceAll('_', ' '))}</span></div>${renderWorkerActions(worker)}<details class="worker-facts"><summary>Permissions &amp; runtime</summary><span class="pill ${mode === 'simulation' ? 'simulated' : mode === 'personal_supported' ? 'blocked' : 'api-mode'}">${esc(modeLabel)}</span><span class="eligibility ${eligible(worker) ? 'eligible' : ''}">${eligible(worker) ? '✓ Eligible' : '○ Not eligible'}</span><p>${esc(reason)}</p><dl><dt>Project</dt><dd>${esc((worker.assigned_projects || []).map(item => item.name + ' · ' + item.state.replaceAll('_', ' ')).join(', ') || 'No current assignment')}</dd><dt>Consent</dt><dd>${worker.opt_in ? 'Opted in' : 'Opted out'}</dd><dt>Capacity</dt><dd>${esc(scope)}</dd><dt>Observation</dt><dd>${esc(capacity.source || 'manual')} · ${esc(capacity.availability || status)}</dd><dt>Quota scope</dt><dd>${esc(capacity.quota_scope || 'Not reported')}</dd><dt>Last seen</dt><dd>${esc(displayDate(worker.last_seen_at))}</dd><dt>Capabilities</dt><dd>${esc(Object.keys(worker.health?.capabilities || {}).filter(key => worker.health.capabilities[key] === true).join(', ') || 'Not reported')}</dd><dt>Parallel tasks</dt><dd>${esc(worker.max_parallel_tasks || 1)}</dd>${capacity.reset_at ? `<dt>Reset reported</dt><dd>${esc(displayDate(capacity.reset_at))}</dd>` : ''}</dl>${worker.health?.reason ? `<p class="health-note">${esc(worker.health.reason)}</p>` : ''}</details>${settings}</article>`;
}

function renderWorkerSelectors(workers, task) {
  const startOptions = workers.filter(worker => worker.execution_mode !== 'personal_supported' &&
    (eligible(worker) || (['organization_api', 'native'].includes(worker.execution_mode) && worker.authorized === true)));
  // Explicit selection can precede the deferred directory response. Keep its identity.
  $('startWorker').innerHTML = '<option value="">Choose an eligible worker automatically</option>' + startOptions.map(worker =>
    `<option value="${esc(worker.id)}">${esc(worker.display_name || worker.name)} · ${esc(executionModes[worker.execution_mode] || worker.execution_mode)}${eligible(worker) ? '' : ' · queued until runtime ready'}</option>`).join('');
  $('startWorker').value = selectedStartWorker;
  const targetOptions = workers.filter(worker => eligible(worker) && worker.id !== task?.worker_id && (!task || worker.execution_mode === task.execution_mode));
  // A freshly enrolled handoff target may not be in the cached directory yet.
  const targetPlaceholder = task?.execution_mode === 'native' ? 'Choose the next local profile explicitly' : 'Choose approved alternate automatically';
  $('switchTarget').innerHTML = `<option value="">${targetPlaceholder}</option>` + targetOptions.map(worker =>
    `<option value="${esc(worker.id)}">${esc(worker.display_name || worker.name)}</option>`).join('');
  $('switchTarget').value = selectedTarget;
  $('switchReason').value = selectedReason;
  $('switchTarget').disabled = busy || !task;
  $('switchReason').disabled = busy || !task;
  $('startWorker').disabled = busy || !!task;
}

function canDispatch(worker) {
  const lock=RelayIDE.getState()?.lock;
  return worker.eligible && (!lock || lock.mine) && !projectTasks.some(task=>!terminalStates.has(task.state) && task.state!=='queued');
}

function renderWorkerActions(worker) {
  if(!overview?.features?.agent_management)return '<p class="permission-note">Restart the Relay backend to enable direct assignments and agent management.</p>';
  const observed=worker.observation || {},task=observed.current_task,latest=observed.latest_task,action=observed.last_action;
  const run=observed.latest_run;
  const elapsed=run?.started_at ? Math.max(0,Math.floor(((run.finished_at?Date.parse(run.finished_at):Date.now())-Date.parse(run.started_at))/1000))+'s' : 'No execution observed';
  return `<div class="agent-observation"><strong>${esc(task?.objective || latest?.objective || 'No assigned task')}</strong><small>${esc((task || latest)?.project_name || 'No project')} · ${esc((task || latest)?.conversation_title || 'No conversation')}</small><div class="agent-last-action">${esc(action ? 'Last observed: '+action.event_type+' · '+action.content.slice(0,180) : 'No observable execution event recorded')}</div><small>${esc(displayDate(action?.timestamp))} · ${esc(observed.phase || 'idle')} · ${esc(elapsed)}</small><div class="agent-last-action">${esc(observed.last_command ? 'Last command: '+observed.last_command.content.slice(0,150) : 'No command recorded for this task')}<br>${observed.reported_changed_files?.length ? esc(observed.reported_changed_files.length+' files reported changed') : 'No file-change events reported'}${observed.checkpoint ? '<br>Checkpoint '+esc(observed.checkpoint.git_commit.slice(0,10)) : ''}</div><small>Provider: ${esc(worker.execution_mode==='simulation'?'Simulated fixture':'Codex')} · Profile: ${esc(worker.native_profile?.toUpperCase() || worker.execution_mode)} · Model: ${esc(observed.model || 'Not reported')}</small></div><div class="agent-controls"><button class="primary compact" data-assign-worker="${esc(worker.id)}" ${!worker.authorized || !currentCanWrite() || busy ? 'disabled' : ''} title="${esc(worker.assignment_reason || worker.eligibility_reason)}">${canDispatch(worker)?'Assign Task':'Queue Task'}</button>${latest?`<button class="outline compact" data-agent-task="${esc(latest.id)}" data-task-project="${esc(latest.project_id)}" data-task-conversation="${esc(latest.conversation_id)}">View activity</button>`:''}${canManage(worker)?`<button class="quiet" data-remove-project="${esc(worker.id)}" ${!worker.permitted_project_ids.includes(project)?'disabled':''}>Remove from project</button><button class="quiet danger" data-remove-worker="${esc(worker.id)}">Remove enrollment</button>`:''}</div>`;
}

async function openTask(identity,conversationId,projectId=project) {
  if(busy)return;
  if(projectId!==project){await RelayIDE.leaveProject(true);project=projectId;loading=false;invalidateView();selectedConversation=conversationId;await load(true);}
  else await selectConversation(conversationId);
  if(identity)RelayIDE.pane('agents');
}

function renderProjectTasks() {
  $('activeTaskCount').textContent=projectTasks.filter(task=>!terminalStates.has(task.state)).length+' active / queued';
  $('projectTasks').innerHTML=projectTasks.length?projectTasks.map(task=>{
    const worker=projectWorkers().find(item=>item.id===(task.worker_id || task.requested_worker_id));
    const write=currentCanWrite() && !busy, paused=task.state==='handoff_pending';
    return `<article class="task-entry"><strong>${esc(task.objective)}</strong><small>${esc(workerName(task.worker_id || task.requested_worker_id))} · ${esc(task.state)} · ${esc(task.conversation_title)}</small><div class="agent-controls"><button class="quiet" data-open-task="${esc(task.id)}">History</button>${task.state==='queued'?`<button class="primary compact" data-task-action="tasks/start" data-task="${esc(task.id)}" ${!write || !worker?.eligible?'disabled':''}>Start chosen agent</button>`:''}${task.state==='running'?`<button class="outline compact" data-task-action="pause" data-task="${esc(task.id)}" ${!write?'disabled':''}>Pause / checkpoint</button>`:''}${paused?`<button class="outline compact" data-task-action="resume" data-task="${esc(task.id)}" ${!write || !worker?.eligible?'disabled':''}>Resume same agent</button>`:''}${!terminalStates.has(task.state)?`<button class="quiet danger" data-task-action="cancel" data-task="${esc(task.id)}" ${!write?'disabled':''}>Cancel</button>`:''}<button class="quiet" data-inspect-task="${esc(task.id)}" ${task.state==='queued'?'disabled':''}>Inspect Changes</button></div>${task.last_error?`<p>${esc(task.last_error)}</p>`:''}</article>`;
  }).join(''):'<div class="empty">No project tasks yet.</div>';
  document.querySelectorAll('[data-open-task]').forEach(button=>button.onclick=()=>{const task=projectTasks.find(item=>item.id===button.dataset.openTask);openTask(task.id,task.conversation_id).catch(showError);});
  document.querySelectorAll('[data-task-action]').forEach(button=>button.onclick=()=>{const task=projectTasks.find(item=>item.id===button.dataset.task);if(button.dataset.taskAction==='cancel' && !window.confirm('Cancel this task? Running tools must stop before its checkpoint is released.'))return;action(button.dataset.taskAction,{task_id:task.id,generation:task.generation});});
  document.querySelectorAll('[data-inspect-task]').forEach(button=>button.onclick=()=>RelayIDE.reviewTask(button.dataset.inspectTask).catch(showError));
}

function openAssignment(workerId) {
  const worker=projectWorkers().find(item=>item.id===workerId);if(!worker)return;
  assignmentTarget=workerId;
  $('assignWorkerName').textContent=worker.display_name || worker.name;
  $('assignReadiness').textContent=worker.assignment_reason || worker.eligibility_reason;
  $('assignConversation').innerHTML='<option value="">New independent conversation</option>'+conversations.filter(item=>!item.archived_at && (!item.latest_task || terminalStates.has(item.latest_task.state))).map(item=>`<option value="${esc(item.id)}">${esc(item.title)}</option>`).join('');
  const lock=RelayIDE.getState()?.lock;
  $('assignQueue').checked=!worker.eligible || projectTasks.some(task=>!terminalStates.has(task.state) && task.state!=='queued') || !!(lock && !lock.mine);
  $('assignPrompt').value='';$('assignError').textContent='';$('assignDialog').showModal();
}
$('closeAssign').onclick=()=>$('assignDialog').close();
$('assignForm').onsubmit=async event=>{
  event.preventDefault();const at=viewVersion,selected=project,worker=assignmentTarget;
  $('submitAssignment').disabled=true;
  const immediate=!$('assignQueue').checked;
  try {
    if(immediate)await RelayIDE.prepareForAgent();
    const result=await api('tasks/assign',{project_id:selected,worker_id:worker,prompt:$('assignPrompt').value,queue:$('assignQueue').checked,...($('assignConversation').value?{conversation_id:$('assignConversation').value}:{}),request_id:crypto.randomUUID()});
    sessionStorage.setItem('relay-conversation:'+overview.user.id+':'+selected,result.conversation_id);
    if(at!==viewVersion){$('assignDialog').close();return;}
    await openTask(result.id,result.conversation_id,selected);await load(true);$('assignDialog').close();
    showNotice(result.state==='queued'?'Task queued for the chosen agent. Start it explicitly when the project and agent are ready.':'Task dispatched to the chosen agent. Follow its recorded execution here.');
  }catch(error){$('assignError').textContent=error.message;}
  finally{if(immediate)await RelayIDE.finishTransition().catch(showError);$('submitAssignment').disabled=false;}
};

function eventOrigin(event) {
  let metadata = {};
  try { metadata = typeof event.metadata === 'string' ? JSON.parse(event.metadata) : event.metadata || {}; } catch {}
  if (metadata.origin === 'codex') return metadata.execution_mode === 'native' ? 'NATIVE LOCAL' : 'ORG API';
  if (metadata.origin === 'simulation') return 'SIMULATED';
  if (metadata.origin === 'human' || event.role === 'user') return metadata.execution_mode === 'native' ? 'HUMAN / NATIVE LOCAL' : metadata.execution_mode === 'organization_api' ? 'HUMAN / ORG API' : 'HUMAN / SIMULATED';
  return metadata.execution_mode === 'native' ? 'RELAY / NATIVE LOCAL' : metadata.execution_mode === 'organization_api' ? 'RELAY / API TASK' : 'RELAY';
}

function eventFacts(event) {
  let metadata = {};
  try { metadata = typeof event.metadata === 'string' ? JSON.parse(event.metadata) : event.metadata || {}; } catch {}
  const facts = [];
  if (metadata.channel) facts.push(metadata.channel);
  if (metadata.exit_code !== null && metadata.exit_code !== undefined) facts.push('exit ' + metadata.exit_code);
  if (metadata.duration_ms) facts.push(metadata.duration_ms + ' ms');
  if (metadata.error_category) facts.push(metadata.error_category);
  return facts.length ? '<div class="event-head">' + esc(facts.join(' · ')) + '</div>' : '';
}

function render() {
  if (!overview || !detail) return;
  const currentProject = overview.projects.find(item => item.id === project);
  $('projectName').textContent = currentProject?.name || project;
  $('conversationId').textContent = detail.conversation.id;
  $('authBadge').textContent = overview.auth?.mode === 'local_demo'
    ? '● Local demo login · not production authentication' : '● Signed in · ' + overview.user.name;
  const task = activeTask();
  const workers = projectWorkers();
  $('taskState').textContent = task?.state || detail.tasks[0]?.state || 'Ready';
  $('pauseAfterTurn').disabled = busy || !!task;
  if (authMode === 'local-owner') $('authBadge').textContent = '● NATIVE LOCAL · owner ' + overview.user.name;
  renderConversation();
  renderConversations();
  document.body.classList.toggle('management-ready',!!overview.features?.agent_management);
  const workerMarkup=workers.length?workers.map(worker=>renderWorker(worker,task)).join(''):(detail.workersLoaded?'<div class="empty">No enrolled workers. Use Add Agent to enroll a supported profile.</div>':'<div class="empty">Loading authorized worker readiness…</div>');
  if($('workers').innerHTML!==workerMarkup)$('workers').innerHTML=workerMarkup;
  const nativeWorkerCount = workers.filter(worker => worker.execution_mode === 'native').length;
  const hasOrgWorkers = workers.some(worker => worker.execution_mode === 'organization_api');
  const liveNative = workers.filter(worker => worker.execution_mode === 'native' && worker.health?.available).length;
  const liveApi = workers.some(worker => worker.execution_mode === 'organization_api' && worker.health?.available);
  const simulationCount = workers.filter(worker => worker.execution_mode === 'simulation').length;
  $('connectionTitle').textContent = nativeWorkerCount ? `NATIVE LOCAL · ${nativeWorkerCount} enrolled worker${nativeWorkerCount === 1 ? '' : 's'}` : liveApi ? 'ORG API runtime configured' : hasOrgWorkers ? 'ORG API runtime unavailable' : `${simulationCount} simulated worker${simulationCount === 1 ? '' : 's'}`;
  $('connectionDetail').textContent = nativeWorkerCount ? 'Local App Servers · owner initiated · availability and actual results recorded per worker/task' : liveApi ? 'Backend health only; each execution reports its own result' : 'Select a configured native or API worker for live execution';
  $('connectionDot').classList.toggle('api-connected', !!liveNative || liveApi);
  const unsupported = overview.personal_subscription || detail.personal_subscription;
  if (unsupported?.reason) $('personalReason').textContent = unsupported.reason;
  $('handoffs').innerHTML = detail.handoffs?.length ? detail.handoffs.map(handoff => `<div class="handoff-item"><strong>${esc(workerName(handoff.source))} → ${esc(handoff.target ? workerName(handoff.target) : 'Waiting for an eligible worker')}</strong>${esc(handoff.reason || handoff.reason_code)}<small>Authorized by ${esc(overview.users.find(user => user.id === handoff.authorized_by)?.name || handoff.authorized_by || 'Recorded system action')}<br>Checkpoint ${esc(handoff.checkpoint?.slice(0, 12) || 'Pending')} · Context v${esc(handoff.context_version || 'pending')}<br>${esc(handoff.result || handoff.status)} · ${esc(displayDate(handoff.timestamp))}</small></div>`).join('') : '<div class="empty">No handoffs yet.<br>Use the demo to carry a saved objective from Worker A to Worker B.</div>';
  ['switch', 'cancel'].forEach(id => $(id).disabled = busy || !task);
  $('limit').disabled = busy || !task || workers.find(worker => worker.id === task.worker_id)?.execution_mode !== 'simulation';
  $('continue').disabled = busy || task?.state !== 'running' || ['organization_api', 'native'].includes(task?.execution_mode);
  $('demo').disabled = busy || !!task;
  document.querySelector('.send').disabled = busy || !!task;
  renderWorkerSelectors(workers, task);
  if (task?.execution_mode === 'native') {
    const source = workers.find(worker => worker.id === task.worker_id);
    const exhausted = source?.capacity?.availability === 'quota_blocked';
    $('switch').disabled = busy || !selectedTarget || exhausted;
    $('resume').disabled = busy || exhausted || task.state !== 'handoff_pending';
  }
  const selectedMode = workers.find(worker => worker.id === selectedStartWorker)?.execution_mode;
  $('composerNote').textContent = selectedMode === 'native'
    ? 'NATIVE CODEX · Your local ChatGPT profile · Choose handoff targets explicitly'
    : selectedMode === 'organization_api'
    ? 'ORGANIZATION API · Shared organization quota · Unavailable work queues until runtime is ready'
    : selectedMode === 'simulation'
      ? 'SIMULATION · Predefined validator demo · No API credentials required'
    : authMode==='local-owner' ? 'Choose a local worker in Agents to start your next task.' : 'Select a worker to see its execution mode.';
  renderArtifact();
  bindWorkerSettings();
  renderProjectTasks();
  if(busy)document.querySelectorAll('#workers button,#workers input,#workers select,#projectTasks button,#conversationList button').forEach(control=>control.disabled=true);
  renderNativeProfiles();
  setControlsDisabled(busy);
  const writable = currentCanWrite();
  document.querySelectorAll('[data-rename-conversation],[data-archive-conversation]').forEach(button=>{if(!overview.features?.agent_management){button.disabled=true;button.title='Restart Relay to enable conversation management';}});
  $('pause').disabled = busy || !task || !writable || task.state!=='running';
  const sourceWorker = workers.find(worker => worker.id === task?.worker_id);
  $('resume').disabled = busy || !task || !writable || task.state !== 'handoff_pending' || sourceWorker?.capacity?.availability === 'quota_blocked' || !eligible(sourceWorker || {});
  $('handoffPhases').disabled = busy || !!task || !writable;
  $('demo').hidden = authMode !== 'demo';
  if (!writable) {
    ['switch', 'cancel', 'limit', 'continue'].forEach(id => $(id).disabled = true);
    document.querySelector('.send').disabled = true;
  }
  if (detail.conversation.archived_at) document.querySelector('.send').disabled=true;
  if (authMode==='local-owner' && !selectedStartWorker) document.querySelector('.send').disabled=true;
  RelayIDE.update({project,user:overview.user.id,conversation:detail.conversation.id,projectName:currentProject?.name || project,
    worker:task?.worker_id ? workerName(task.worker_id) : detail.tasks[0]?.worker_id ? workerName(detail.tasks[0].worker_id) : '',
    developmentTerminal:!!overview.features?.development_terminal,automaticWorkflow:!!overview.features?.automatic_workflow,managementAvailable:!!overview.features?.agent_management,checkpoints:detail.checkpoints,state:task?.state || detail.tasks[0]?.state,objective:task?.objective || detail.tasks[0]?.objective,checkpoint:detail.workspace.checkpoint,
    selectedWorker:workers.find(worker => worker.id === selectedStartWorker)?.display_name || workers.find(worker => worker.id === selectedStartWorker)?.name}).catch(showError);
}

function renderConversation() {
  if (!detail) return;
  const state=activeTask()?.state || detail.tasks[0]?.state || 'ready';
  if(activityLoading && !events.length)$('timeline').innerHTML='<div class="agent-empty">Loading recent conversation activity…</div>';
  else activityView.render(events,detail.conversation.id,state);
  $('loadOlderActivity').hidden=!hasOlderActivity;
  const latest=activityView.model.groups.slice().reverse().find(group => group.kind==='operation' && !group.complete);
  $('currentOperation').textContent=state==='running' ? latest ? latest.command || 'Receiving tool output' : 'Waiting for the next observable event' : state==='handoff_pending' ? 'Checkpoint saved · ready for explicit handoff' : state==='completed' ? 'Task completed' : state==='pausing' ? 'Stopping at a safe checkpoint…' : 'Waiting for your next task';
  $('operationIndicator').classList.toggle('spinning',state==='running');
  $('rawEventCount').textContent=events.length;
}

function renderConversations() {
  $('conversationList').innerHTML=conversations.filter(item=>!item.archived_at || $('showArchived').checked || item.id===detail?.conversation.id).map(item => `<div class="conversation-row"><button class="conversation ${item.id===detail?.conversation.id ? 'selected' : ''}" data-conversation="${esc(item.id)}"><span>◉</span><div><strong>${esc(item.title)}${item.archived_at?' · Archived':''}</strong><small>${esc(item.latest_task ? workerName(item.latest_task.worker_id || item.latest_task.requested_worker_id)+' · '+item.latest_task.state : 'No task yet')}<br>${esc(item.updated_at ? new Date(item.updated_at).toLocaleDateString() : '')} · ${esc(item.event_count)} events saved<br>${esc((item.participants || []).map(worker=>worker.name).join(', ') || 'No agent participants yet')}</small></div></button><div class="conversation-actions"><button data-rename-conversation="${esc(item.id)}" ${!currentCanWrite()?'disabled':''}>Rename</button><button data-archive-conversation="${esc(item.id)}" ${!currentCanWrite()?'disabled':''}>${item.archived_at?'Restore':'Archive'}</button></div></div>`).join('');
  document.querySelectorAll('[data-conversation]').forEach(button => button.onclick=() => selectConversation(button.dataset.conversation).catch(showError));
  document.querySelectorAll('[data-rename-conversation]').forEach(button=>button.onclick=async()=>{
    const item=conversations.find(row=>row.id===button.dataset.renameConversation),title=window.prompt('Conversation title',item.title);if(!title?.trim())return;
    try{await api('ide/conversations/update',{project_id:project,conversation_id:item.id,title:title.trim()});await load(true);}catch(error){showError(error);}
  });
  document.querySelectorAll('[data-archive-conversation]').forEach(button=>button.onclick=async()=>{
    const item=conversations.find(row=>row.id===button.dataset.archiveConversation),archived=!item.archived_at;
    if(archived && !window.confirm('Archive '+item.title+'? Its messages, tasks and checkpoints remain available.'))return;
    try{await api('ide/conversations/update',{project_id:project,conversation_id:item.id,archived,confirmed:archived});await load(true);}catch(error){showError(error);}
  });
}
$('showArchived').onchange=renderConversations;

async function selectConversation(identity) {
  if (busy || identity===detail?.conversation.id) return;
  await RelayIDE.flushDrafts();
  loading=false;invalidateView();selectedConversation=identity;
  await load(true);
}

async function newConversation() {
  if (!project || !currentCanWrite()) return;
  const title=window.prompt('Conversation title');if (!title?.trim()) return;
  const created=await api('ide/conversations',{project_id:project,title:title.trim()});
  await selectConversation(created.id);
}

function bindWorkerSettings() {
  document.querySelectorAll('[data-assign-worker]').forEach(button=>button.onclick=()=>openAssignment(button.dataset.assignWorker));
  document.querySelectorAll('[data-agent-task]').forEach(button=>button.onclick=()=>openTask(button.dataset.agentTask,button.dataset.taskConversation,button.dataset.taskProject).catch(showError));
  document.querySelectorAll('[data-remove-worker]').forEach(button=>button.onclick=()=>{
    if(window.confirm('Remove this agent enrollment? Historical tasks and checkpoints are retained. Native authentication is untouched. Cancel or hand off unfinished tasks first.'))action('workers/remove',{worker_id:button.dataset.removeWorker,confirmed:true},'Agent enrollment removed. History and native login are preserved.');
  });
  document.querySelectorAll('[data-remove-project]').forEach(button=>button.onclick=()=>{
    if(window.confirm('Remove this agent authorization from this project? Active work will stop and checkpoint safely. Enrollment and other project permissions remain.'))action('workers/revoke',{worker_id:button.dataset.removeProject,project_id:project},'Project authorization revoked; any affected task stops safely.');
  });
  document.querySelectorAll('[data-worker-form]').forEach(form => {
    form.onsubmit = event => {
      event.preventDefault();
      const worker = projectWorkers().find(item => item.id === form.dataset.workerForm);
      const visibleProjects = new Set(overview.projects.map(item => item.id));
      const unchangedHiddenProjects = (worker.permitted_project_ids || []).filter(id => !visibleProjects.has(id));
      action('workers/configure', {
        worker_id: form.dataset.workerForm,
        display_name:form.elements.display_name.value,
        ...(!form.elements.state.disabled && worker.execution_mode !== 'personal_supported'
          ? {state: form.elements.state.value} : {}),
        opt_in: form.elements.opt_in.checked,
        project_ids: [...unchangedHiddenProjects, ...Array.from(form.querySelectorAll('input[name="project_ids"]:checked'), input => input.value)]
      }, 'Worker settings saved. Active execution, if affected, stops at a safe checkpoint.');
    };
  });
  document.querySelectorAll('[data-revoke]').forEach(button => {
    button.onclick = () => action('workers/revoke', {worker_id: button.dataset.revoke}, 'Worker opted out. Its active task is preserved for a permitted handoff.');
  });
}

function renderArtifact() {
  if (!detail) return;
  if(detail.light && document.querySelector('.workspace-records').open){$('artifactBody').textContent='Loading saved workspace records…';loadArtifacts();return;}
  const workspace = detail.workspace;
  const context = detail.contexts?.[0];
  if (tab === 'context') {
    $('artifactBody').innerHTML = `<pre>${esc(context ? JSON.stringify(context, null, 2) : 'Working context is created when a task starts.')}</pre>`;
  } else if (tab === 'handoff') {
    $('artifactBody').innerHTML = `<pre>${esc(workspace.files['.relay/HANDOFF.md'] || 'HANDOFF.md will appear here after the first checkpoint.')}</pre>`;
  } else if (tab === 'checkpoints') {
    $('artifactBody').innerHTML = detail.checkpoints?.length ? detail.checkpoints.map(checkpoint => `<div class="checkpoint-item"><strong>${esc(checkpoint.git_commit || checkpoint.checkpoint || checkpoint.commit_id || checkpoint.id)}</strong><small>${esc(checkpoint.reason || checkpoint.message || 'Workspace checkpoint')} · ${esc(displayDate(checkpoint.created_at || checkpoint.timestamp))}</small></div>`).join('') : `<pre>${esc('Current Git checkpoint: ' + workspace.checkpoint)}</pre>`;
  } else {
    const content = selectedFile === 'diff' ? (workspace.task_diff ?? workspace.diff) : workspace.files[selectedFile] || workspace.files['validator.py'] || workspace.files['README.md'];
    $('artifactBody').innerHTML = `<div class="file-list">${Object.keys(workspace.files).map(name => `<button data-file="${esc(name)}">▤ ${esc(name)}</button>`).join('')}<button data-file="diff">± Latest commit diff</button></div><pre id="fileContent">${esc('Git checkpoint ' + workspace.checkpoint.slice(0, 12) + '\n\n' + (content || 'No changes yet.'))}</pre>`;
    document.querySelectorAll('[data-file]').forEach(button => button.onclick = () => {
      selectedFile = button.dataset.file;
      $('fileContent').textContent = selectedFile === 'diff' ? (workspace.task_diff ?? workspace.diff) || 'No changes yet.' : workspace.files[selectedFile];
    });
  }
}

async function action(method, data, notice = '') {
  if (busy || !detail) return;
  const version = viewVersion;
  busy = true;
  $('error').hidden = true;
  showNotice('');
  render();
  try {
    if(['prompt','switch','resume','tasks/start','demo'].includes(method))await RelayIDE.prepareForAgent();
    await api(method, {...data, request_id: crypto.randomUUID()});
    if (version !== viewVersion) return;
    await load(true);
    if (version === viewVersion) {
      $('timeline').scrollTop = $('timeline').scrollHeight;
      showNotice(notice);
    }
  } catch (failure) {
    if (version === viewVersion) {
      showError(failure);
      await load(true).catch(showError);
    }
  } finally {
    await RelayIDE.finishTransition().catch(showError);
    busy = false;
    if (version === viewVersion) render();
  }
}

$('composer').onsubmit = event => {
  event.preventDefault();
  if (detail) action('prompt', {
    conversation_id: detail.conversation.id,
    prompt: $('prompt').value,
    pause_after_turn: $('pauseAfterTurn').checked,
    native_phases: Number($('handoffPhases').value),
    ...(selectedStartWorker ? {worker_id: selectedStartWorker} : {})
  });
};
$('startWorker').onchange = () => { selectedStartWorker = $('startWorker').value; render(); };
$('switchTarget').onchange = () => { selectedTarget = $('switchTarget').value; render(); };
$('switchReason').onchange = () => { selectedReason = $('switchReason').value; };
$('switch').onclick = () => {
  const task = activeTask();
  if (!task) return;
  action('switch', {task_id: task.id, generation: task.generation, reason_code: selectedReason,
    ...(selectedTarget ? {target_worker_id: selectedTarget} : {})});
};
['limit', 'continue', 'cancel', 'pause', 'resume'].forEach(method => $(method).onclick = () => {
  const task = activeTask();
  if (task) action(method, {task_id: task.id, generation: task.generation});
});
$('demo').onclick = () => action('demo', {project_id: project}, 'Handoff demo recorded. Inspect the worker attribution, checkpoint, and unchanged conversation ID.');
$('project').onchange = async () => {
  if (busy) return;
  const next=$('project').value;
  try{await RelayIDE.leaveProject(true);}catch(error){$('project').value=project;showError(error);return;}
  project = next;
  loading = false;
  invalidateView();
  $('error').hidden = true;
  showNotice('');
  await load(true).catch(showError);
};
$('user').onchange = login;
$('newProject').onclick = async () => {
  if (busy) return;
  const name = window.prompt('Project name');
  if (!name?.trim()) return;
  busy = true;
  setControlsDisabled(true);
  try {
    await RelayIDE.leaveProject(true);
    const created = await api('projects', {name, request_id: crypto.randomUUID()});
    project = created.id;
    loading = false;
    invalidateView();
    await load(true);
  } catch (failure) {
    showError(failure);
  } finally {
    await RelayIDE.finishTransition().catch(showError);
    busy = false;
    setControlsDisabled(false);
    if (detail) render();
  }
};
document.querySelectorAll('[data-tab]').forEach(button => button.onclick = () => {
  tab = button.dataset.tab;
  document.querySelectorAll('[data-tab]').forEach(item => item.classList.toggle('active', item === button));
  renderArtifact();
});
$('more').onclick = async () => {
  if (!detail || busy || $('more').disabled) return;
  const version = viewVersion;
  const conversation = detail.conversation.id;
  $('more').disabled = true;
  try {
    const page = await api(`conversations/${encodeURIComponent(conversation)}/events?after=${historyAfter}&limit=100`);
    if (version !== viewVersion) return;
    historyEvents.push(...page.events);
    historyAfter = page.next_after;
    renderRawHistory();
    $('more').textContent = page.events.length === 100 ? 'Load next 100 events' : 'Check for more events';
  } catch (failure) {
    if (version === viewVersion) showError(failure);
  } finally {
    $('more').disabled = false;
  }
};

function renderRawHistory() {
  const text=$('eventFilter').value.toLowerCase(),type=$('eventTypeFilter').value;
  const filtered=historyEvents.filter(event=>(!type || event.event_type===type) && (!text || JSON.stringify(event).toLowerCase().includes(text)));
  $('history').textContent=filtered.map(event=>JSON.stringify(event,null,2)).join('\n\n');
}
$('loadOlderActivity').onclick=async()=>{
  if(activityLoading || !detail || !events.length)return;
  const version=viewVersion,conversation=detail.conversation.id,before=events[0].sequence;
  $('loadOlderActivity').disabled=true;
  try {
    const page=await api(`conversations/${encodeURIComponent(conversation)}/events?limit=100&before=${before}`);
    if(version!==viewVersion)return;
    const merged=new Map([...page.events,...events].map(event=>[event.event_id,event]));events=[...merged.values()].sort((a,b)=>a.sequence-b.sequence);
    hasOlderActivity=page.has_older===true;activityView.showOlder=true;renderConversation();
  }catch(error){showError(error);}finally{$('loadOlderActivity').disabled=false;}
};
document.querySelector('.workspace-records').addEventListener('toggle',()=>{if(document.querySelector('.workspace-records').open)loadArtifacts();});
$('eventFilter').oninput=renderRawHistory;
$('eventTypeFilter').onchange=renderRawHistory;
$('jumpLatest').onclick=()=>{activityView.windowEnd=null;renderConversation();$('timeline').scrollTop=$('timeline').scrollHeight;$('jumpLatest').hidden=true;};
$('timeline').onscroll=()=>{$('jumpLatest').hidden=$('timeline').scrollHeight-$('timeline').clientHeight-$('timeline').scrollTop<100;};

function renderEnrollmentMode() {
  const mode = $('workerMode').value;
  const organizationOption = $('workerOwner').querySelector('option[value="org"]');
  organizationOption.disabled = overview?.user.role !== 'admin';
  $('workerOwner').disabled = ['organization_api', 'native'].includes(mode);
  $('nativeProfileLabel').hidden = mode !== 'native';
  $('nativeProfile').hidden = mode !== 'native';
  $('nativeProfile').disabled = mode !== 'native';
  if (mode === 'native') {
    const existing=(nativeProfiles?.profiles || []).filter(profile=>profile.authentication==='chatgpt' && profile.available && !profile.worker_id);
    $('nativeProfile').innerHTML=existing.map(profile=>`<option value="${esc(profile.profile)}">${esc(profile.profile.toUpperCase())} · authenticated and preflight ready</option>`).join('') || '<option value="">No unenrolled authenticated profiles</option>';
    $('enrollmentForm').querySelector('[type=submit]').disabled=!existing.length;
    $('workerOwner').value = 'user';
    $('enrollmentModeNote').textContent = 'Only existing authenticated, ready, unenrolled profiles appear here. For setup, run python3 scripts/native_profiles.py init in the Relay directory, then run its printed login commands privately in your terminal. Keep credentials there. Existing enrolled profiles are managed in Agents.';
  } else if (mode === 'organization_api') {
    $('enrollmentForm').querySelector('[type=submit]').disabled=false;
    $('workerOwner').value = 'org';
    $('enrollmentModeNote').textContent = 'Organization API workers require an administrator and server-side credential/runtime configuration. All workers under that organization share API quota.';
  } else {
    $('enrollmentForm').querySelector('[type=submit]').disabled=mode==='personal_supported';
    if (overview?.user.role !== 'admin' || $('workerOwner').value === 'org') $('workerOwner').value = 'user';
    $('enrollmentModeNote').textContent = mode === 'personal_supported'
      ? 'This enrollment remains disabled with unsupported_execution_mode. No provider-supported cross-user delegation mechanism has been verified for Relay.'
      : 'Runs a fixed, deterministic coding demo. Each simulated worker has its own simulated availability; no API key required.';
  }
}
$('enroll').onclick = () => {
  if (!overview) return;
  if(authMode==='local-owner')$('workerMode').value='native';
  renderEnrollmentMode();
  $('enrollProject').disabled = !project;
  $('enrollmentDialog').showModal();
};
$('closeEnrollment').onclick = () => $('enrollmentDialog').close();
$('workerMode').onchange = renderEnrollmentMode;
$('enrollmentForm').onsubmit = async event => {
  event.preventDefault();
  const body = {
    display_name: $('workerName').value,
    execution_mode: $('workerMode').value,
    ...($('workerMode').value === 'native' ? {native_profile: $('nativeProfile').value} : {}),
    owner_type: $('workerOwner').value,
    project_ids: $('enrollProject').checked && project ? [project] : [],
    opt_in: $('enrollOptIn').checked,
    max_parallel_tasks: 1
  };
  $('enrollmentDialog').close();
  await action('workers/enroll', body, body.execution_mode === 'personal_supported'
    ? 'Worker registered as unsupported. Personal subscription execution remains disabled.' : 'Worker enrolled. Eligibility is evaluated against project approval, owner opt-in, capacity, and runtime health.');
};

function renderNativeProfiles(selectedProject = project) {
  $('localNativePanel').hidden = authMode !== 'local-owner';
  if (!nativeProfiles || authMode !== 'local-owner') return;
  $('localNativeOwner').textContent = 'Pinned owner: ' + (overview?.user.name || nativeProfiles.profiles[0]?.owner_name || 'Local owner');
  $('localNativeProfiles').innerHTML = nativeProfiles.profiles.map(profile => `<article class="native-profile"><strong>Codex ${esc(profile.profile.toUpperCase())}</strong><span class="pill">${esc(profile.status)}</span><p class="profile-project">${esc((profile.assigned_projects || []).map(item => item.name).join(', ') || 'No current project')} · ${esc(profile.owner_name || 'Local owner')}</p><p>${esc(profile.reason)}</p><button class="outline" data-native-profile="${esc(profile.profile)}" ${busy || loading || !detail || !selectedProject || !profile.available ? 'disabled' : ''}>${activeTask() ? 'Prepare ' + esc(profile.profile.toUpperCase()) + ' for handoff' : 'Select ' + esc(profile.profile.toUpperCase()) + ' for this project'}</button>${!profile.available && profile.status!=='busy'?`<button class="quiet" data-native-setup="${esc(profile.profile)}">Profile setup / resolve prerequisite</button>`:''}</article>`).join('');
  document.querySelectorAll('[data-native-setup]').forEach(button=>button.onclick=()=>{const profile=nativeProfiles.profiles.find(item=>item.profile===button.dataset.nativeSetup);$('nativeSetupReason').textContent='Profile '+profile.profile.toUpperCase()+': '+profile.reason;$('nativeSetupDialog').showModal();});
  document.querySelectorAll('[data-native-profile]').forEach(button => button.onclick = async () => {
    if (busy || !selectedProject) return;
    busy = true;
    try {
      const worker = await api('native/profiles/enroll', {profile: button.dataset.nativeProfile, project_id: selectedProject});
      detail.workers=[...projectWorkers().filter(item=>item.id!==worker.id),worker];overview.workers=detail.workers;
      if (activeTask()) selectedTarget = worker.id;
      else selectedStartWorker = worker.id;
      await load(true);
      showNotice(activeTask() ? 'Native successor prepared. Use Request Handoff to authorize its execution explicitly.' : 'NATIVE LOCAL profile selected. Enter your coding objective; no simulation will be substituted.');
    } catch (failure) { showError(failure); }
    finally { busy = false; if (detail) render(); }
  });
}

$('closeNativeSetup').onclick=()=>$('nativeSetupDialog').close();

function currentCanWrite() {
  return overview?.projects?.find(item => item.id === project)?.can_write !== false;
}

function showWorkspaceIdentity(value) {
  document.body.classList.remove('signed-out');
  $('accountPanel').hidden = true;
  const demo = value.auth?.mode === 'local_demo';
  $('user').hidden = !demo;
  $('identityLabel').hidden = !demo;
  $('signedInName').hidden = demo;
  $('signedInName').textContent = value.user.name;
  document.querySelector('.profile > .avatar').textContent = value.user.name.split(/\s+/).map(part => part[0]).join('').slice(0, 2).toUpperCase();
  $('logout').hidden = demo;
  $('organizationName').textContent = value.organizations[0]?.name || 'Relay';
  $('organizationMode').textContent = authMode === 'local-owner' ? 'Local owner · loopback only' : demo ? 'Local demo organization' : 'Invite-only organization';
  $('inviteMember').hidden = authMode !== 'invite' || value.user.role !== 'admin';
  $('manageMembers').hidden = authMode !== 'invite' || value.user.role !== 'admin';
  $('workerMode').querySelectorAll('option').forEach(option => {
    option.disabled = option.value === 'native' ? authMode !== 'local-owner' || !value.auth.native_enabled : option.value === 'personal_supported' && !demo;
  });
}

function showSignIn() {
  RelayIDE.signOut();
  invalidateView();
  overview = null;
  csrfToken = '';
  document.body.classList.add('signed-out');
  $('accountPanel').hidden = false;
  $('accountPassword').value = '';
}

function renderRegistration() {
  document.querySelectorAll('.registration-field').forEach(item => item.hidden = !registration);
  $('accountName').required = registration;
  $('invitationToken').required = registration;
  $('accountTitle').textContent = registration ? 'Join Relay by invitation' : 'Sign in to Relay';
  $('accountForm').querySelector('button[type="submit"]').textContent = registration ? 'Create Relay account' : 'Sign in';
  $('accountPassword').autocomplete = registration ? 'new-password' : 'current-password';
  $('toggleRegistration').textContent = registration ? 'Use an existing Relay account' : 'Accept a Relay invitation';
}

function subscribe(conversation, after) {
  if (liveSource?.conversation === conversation || typeof EventSource === 'undefined') return;
  if (liveSource) liveSource.close();
  const source = new EventSource(`/api/conversations/${encodeURIComponent(conversation)}/stream?after=${after}`);
  source.conversation = conversation;
  liveSource = source;
  source.onopen=()=>{$('streamStatus').textContent='Live';};
  source.onerror=()=>{$('streamStatus').textContent='Reconnecting';};
  source.onmessage = message => {
    if (lastConversation!==conversation) return;
    let event;try {event=JSON.parse(message.data);} catch {return;}
    if (event.conversation_id!==conversation) return;
    if (!events.some(item=>item.event_id===event.event_id)) {
      events.push(event);events.sort((a,b)=>a.sequence-b.sequence);
      const task=detail?.tasks.find(item=>item.id===event.task_id),meta=RelayActivity.metadata(event);
      if(task){
        if(Number.isInteger(meta.generation) && meta.generation>=task.generation){task.generation=meta.generation;if(event.worker_id)task.worker_id=event.worker_id;}
        if(event.event_type==='state')task.state=event.content;
      }
    }
    if (!activityFrame) activityFrame=requestAnimationFrame(()=>{activityFrame=null;renderConversation();});
    if (!['agent_delta','runtime_output','tool_invocation','tool_output'].includes(event.event_type) && !liveTimer) liveTimer=setTimeout(()=>{
      liveTimer=null;
      if (!busy && !loading && lastConversation===conversation) load().catch(showError);
    },750);
  };
  // EventSource reconnects with Last-Event-ID. Polling remains the fallback.
}

$('toggleRegistration').onclick = () => { if (authMode !== 'invite') return; registration = !registration; renderRegistration(); };
$('accountForm').onsubmit = async event => {
  event.preventDefault();
  const button = $('accountForm').querySelector('button[type="submit"]');
  button.disabled = true;
  $('accountError').textContent = '';
  const email = $('accountEmail').value;
  const password = $('accountPassword').value;
  try {
    if (registration) await api('auth/register', {email, password, name: $('accountName').value, invitation_token: $('invitationToken').value});
    const signedIn = await api('login', {email, password});
    csrfToken = signedIn.csrf_token;
    $('accountPassword').value = '';
    $('invitationToken').value = '';
    project = null;
    await load(true);
  } catch (failure) { $('accountError').textContent = failure.message; }
  finally { button.disabled = false; }
};
$('logout').onclick = async () => {
  try { await RelayIDE.leaveProject();await api('logout', {}); showSignIn(); }
  catch (failure) { showError(failure); }
};
$('inviteMember').onclick = () => {
  $('inviteLink').value = '';
  $('inviteError').textContent = '';
  $('inviteDialog').showModal();
};
$('closeInvite').onclick = () => { $('inviteLink').value = ''; $('inviteDialog').close(); };
$('inviteForm').onsubmit = async event => {
  event.preventDefault();
  try {
    if (!project || !detail) throw new Error('Wait for an authorized project to finish loading before creating its invitation.');
    const invitation = await api('invitations', {email: $('inviteEmail').value, project_id: project, project_role: $('inviteRole').value});
    $('inviteLink').value = invitation.invitation_url;
  } catch (failure) { $('inviteError').textContent = failure.message; }
};
$('manageMembers').onclick = async () => {
  try {
    const data = await api('organization/members');
    $('memberUser').innerHTML = data.members.map(member => `<option value="${esc(member.id)}">${esc(member.name)} · ${esc(member.email)}</option>`).join('');
    $('membersError').textContent = '';
    $('membersDialog').showModal();
  } catch (failure) { showError(failure); }
};
$('closeMembers').onclick = () => $('membersDialog').close();
$('membersForm').onsubmit = async event => {
  event.preventDefault();
  try {
    await api('membership', {project_id: project, user_id: $('memberUser').value, role: $('memberRole').value});
    $('membersDialog').close();
    await load(true);
  } catch (failure) { $('membersError').textContent = failure.message; }
};

async function init() {
  const config = await api('auth/config');
  authMode = config.mode;
  if (authMode !== 'demo') {
    $('prompt').value = '';
    $('toggleRegistration').hidden = authMode === 'local-owner';
    $('accountTitle').textContent = authMode === 'local-owner' ? 'Sign in as the local owner' : 'Sign in to Relay';
    const fragment = new URLSearchParams(location.hash.slice(1));
    if (authMode === 'invite' && fragment.has('invite')) {
      registration = true;
      $('invitationToken').value = fragment.get('invite');
      history.replaceState(null, '', location.pathname);
      renderRegistration();
    }
    try { const session = await api('auth/session'); csrfToken = session.csrf_token; await load(true); }
    catch (failure) { if (failure.status === 401) showSignIn(); else showError(failure); }
  } else {
    try { await load(); }
    catch (failure) { if (failure.status === 403) await login(); else showError(failure); }
  }
}
RelayIDE.connect({api,showError,showNotice,openTask:task=>{if(task)openTask(task.id,task.conversation_id).catch(showError);},refresh:()=>load(true),canWrite:currentCanWrite,newConversation:()=>newConversation().catch(showError),loadHistory:()=>{if (!historyEvents.length) $('more').onclick();}});
init().catch(showError);
setInterval(() => {
  const editingWorker = document.activeElement?.closest('.worker-settings');
  if (!document.body.classList.contains('signed-out') && !busy && !loading && !$('enrollmentDialog').open && !$('assignDialog').open && !editingWorker && !document.querySelector('.worker-settings[open]')) load().catch(showError);
}, 5000);
