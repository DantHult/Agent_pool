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

const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[character]));
const displayDate = value => value ? new Date(value).toLocaleString() : 'Not reported';

async function api(path, body) {
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
    if (response.status === 401 && authMode !== 'demo') showSignIn();
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
  const worker = projectWorkers().find(item => item.id === id);
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
  selectedStartWorker = '';
  selectedTarget = '';
  clearHistory();
  $('timeline').innerHTML = '<div class="empty">Loading authorized project history…</div>';
  $('artifactBody').textContent = '';
  $('handoffs').textContent = '';
  $('workers').textContent = '';
}

function setControlsDisabled(disabled) {
  $('user').disabled = disabled;
  $('project').disabled = disabled;
  $('newProject').disabled = disabled || overview?.user.role === 'viewer';
  $('enroll').disabled = disabled;
}

async function login() {
  if (busy) return;
  busy = true;
  loading = false;
  setControlsDisabled(true);
  invalidateView();
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
  const version = viewVersion;
  const serial = ++refreshSerial;
  loading = true;
  try {
    const nextOverview = await api('overview');
    showWorkspaceIdentity(nextOverview);
    if (version !== viewVersion || serial !== refreshSerial) return;
    const nextProject = nextOverview.projects.some(item => item.id === project)
      ? project : nextOverview.projects[0]?.id;
    if (authMode === 'local-owner') {
      nativeProfiles = await api('native/profiles' + (nextProject ? '?project_id=' + encodeURIComponent(nextProject) : ''));
      renderNativeProfiles(nextProject);
    }
    if (!nextProject) {
      overview = nextOverview;
      project = null;
      $('project').innerHTML = '';
      $('timeline').innerHTML = '<div class="empty">No projects are authorized for this identity.</div>';
      return;
    }
    const nextDetail = await api('projects/' + encodeURIComponent(nextProject));
    if (version !== viewVersion || serial !== refreshSerial) return;
    const conversation = nextDetail.conversation.id;
    const nextEvents = lastConversation === conversation ? [...events] : [];
    let after = nextEvents.length ? nextEvents[nextEvents.length - 1].sequence : 0;
    while (true) {
      const page = await api(`conversations/${encodeURIComponent(conversation)}/events?after=${after}&limit=100`);
      if (version !== viewVersion || serial !== refreshSerial) return;
      nextEvents.push(...page.events);
      if (page.events.length < 100) break;
      after = page.next_after;
    }
    if (version !== viewVersion || serial !== refreshSerial) return;
    if (lastConversation !== conversation) {
      clearHistory();
      selectedStartWorker = '';
      selectedTarget = '';
    }
    overview = nextOverview;
    detail = nextDetail;
    project = nextProject;
    events = nextEvents;
    lastConversation = conversation;
    subscribe(conversation, nextEvents.length ? nextEvents[nextEvents.length - 1].sequence : 0);
    $('project').innerHTML = overview.projects.map(item =>
      `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
    $('project').value = project;
    $('user').value = overview.user.id;
    const scroll = $('timeline').scrollTop;
    const nearBottom = $('timeline').scrollHeight - $('timeline').clientHeight - scroll < 40;
    render();
    $('timeline').scrollTop = nearBottom ? $('timeline').scrollHeight : scroll;
  } finally {
    if (version === viewVersion && serial === refreshSerial) {
      loading = false;
      if (authMode === 'local-owner') renderNativeProfiles();
    }
  }
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
  const settings = canManage(worker) ? `<details class="worker-settings" data-settings="${esc(worker.id)}"><summary>Owner settings</summary><form data-worker-form="${esc(worker.id)}"><label>Availability${availabilityControl}</label><label class="checkbox-row"><input type="checkbox" name="opt_in" ${worker.opt_in ? 'checked' : ''}> Opt in to eligible assignments</label><fieldset><legend>Explicit project approvals</legend>${overview.projects.map(item => `<label class="checkbox-row"><input type="checkbox" name="project_ids" value="${esc(item.id)}" ${projects.includes(item.id) ? 'checked' : ''}> ${esc(item.name)}</label>`).join('')}</fieldset><div class="settings-actions"><button class="outline" type="submit">Save settings</button><button class="quiet danger" type="button" data-revoke="${esc(worker.id)}">Opt out & stop safely</button></div></form></details>` : '<div class="permission-note">Settings controlled by the worker owner or administrator</div>';
  return `<article class="worker-entry ${active ? 'active' : ''}"><div class="worker"><span class="worker-icon">⌘</span><div><strong>${esc(worker.display_name || worker.name)}</strong><small>${esc(ownerName(worker))} · ${worker.owner_type === 'org' ? 'Organization owned' : 'User owned'}</small></div><span class="state">${esc(active ? 'active session' : status.replace('_', ' '))}</span></div><div class="worker-facts"><span class="pill ${mode === 'simulation' ? 'simulated' : mode === 'personal_supported' ? 'blocked' : 'api-mode'}">${esc(modeLabel)}</span><span class="eligibility ${eligible(worker) ? 'eligible' : ''}">${eligible(worker) ? '✓ Eligible' : '○ Not eligible'}</span><p>${esc(reason)}</p><dl><dt>Consent</dt><dd>${worker.opt_in ? 'Opted in' : 'Opted out'}</dd><dt>Capacity</dt><dd>${esc(scope)}</dd><dt>Observation</dt><dd>${esc(capacity.source || 'manual')} · ${esc(capacity.availability || status)}</dd><dt>Quota scope</dt><dd>${esc(capacity.quota_scope || 'Not reported')}</dd><dt>Last seen</dt><dd>${esc(displayDate(worker.last_seen_at))}</dd><dt>Capabilities</dt><dd>${esc(Object.keys(worker.health?.capabilities || {}).filter(key => worker.health.capabilities[key] === true).join(', ') || 'Not reported')}</dd><dt>Parallel tasks</dt><dd>${esc(worker.max_parallel_tasks || 1)}</dd>${capacity.reset_at ? `<dt>Reset reported</dt><dd>${esc(displayDate(capacity.reset_at))}</dd>` : ''}</dl>${worker.health?.reason ? `<p class="health-note">${esc(worker.health.reason)}</p>` : ''}</div>${settings}</article>`;
}

function renderWorkerSelectors(workers, task) {
  const startOptions = workers.filter(worker => worker.execution_mode !== 'personal_supported' &&
    (eligible(worker) || (['organization_api', 'native'].includes(worker.execution_mode) && worker.authorized === true)));
  if (!startOptions.some(worker => worker.id === selectedStartWorker)) selectedStartWorker = '';
  $('startWorker').innerHTML = '<option value="">Choose an eligible worker automatically</option>' + startOptions.map(worker =>
    `<option value="${esc(worker.id)}">${esc(worker.display_name || worker.name)} · ${esc(executionModes[worker.execution_mode] || worker.execution_mode)}${eligible(worker) ? '' : ' · queued until runtime ready'}</option>`).join('');
  $('startWorker').value = selectedStartWorker;
  const targetOptions = workers.filter(worker => eligible(worker) && worker.id !== task?.worker_id && (!task || worker.execution_mode === task.execution_mode));
  if (!targetOptions.some(worker => worker.id === selectedTarget)) selectedTarget = '';
  const targetPlaceholder = task?.execution_mode === 'native' ? 'Choose the next local profile explicitly' : 'Choose approved alternate automatically';
  $('switchTarget').innerHTML = `<option value="">${targetPlaceholder}</option>` + targetOptions.map(worker =>
    `<option value="${esc(worker.id)}">${esc(worker.display_name || worker.name)}</option>`).join('');
  $('switchTarget').value = selectedTarget;
  $('switchReason').value = selectedReason;
  $('switchTarget').disabled = busy || !task;
  $('switchReason').disabled = busy || !task;
  $('startWorker').disabled = busy || !!task;
}

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
  document.querySelector('.conversation div').innerHTML = esc(currentProject?.name || project) + '<small>One conversation. Every worker.</small>';
  $('conversationId').textContent = detail.conversation.id;
  $('authBadge').textContent = overview.auth?.mode === 'local_demo'
    ? '● Local demo login · not production authentication' : '● Signed in · ' + overview.user.name;
  const task = activeTask();
  const workers = projectWorkers();
  $('taskState').textContent = task?.state || detail.tasks[0]?.state || 'Ready';
  $('pauseAfterTurn').disabled = busy || !!task;
  if (authMode === 'local-owner') $('authBadge').textContent = '● NATIVE LOCAL · owner ' + overview.user.name;
  $('timeline').innerHTML = events.length ? events.map(event => `<article class="event ${esc(event.event_type)} ${esc(event.role)}"><div class="event-head"><span>${esc(event.worker_id ? workerName(event.worker_id) : event.role === 'user' ? 'YOU' : 'RELAY')} · ${esc(event.event_type)} · ${esc(eventOrigin(event))}</span><time datetime="${esc(event.timestamp)}">${esc(displayDate(event.timestamp))}</time></div>${esc(event.content)}${eventFacts(event)}</article>`).join('') : '<div class="welcome"><div class="welcome-icon">⇄</div><h2>A fresh start. A continuous thread.</h2><p>Choose an approved worker and give it a task. Code, tests, and visible history stay with this project.</p><div class="steps"><span>01 Start a task</span><span>02 Save a checkpoint</span><span>03 Pass the context</span></div></div>';
  $('workers').innerHTML = workers.length ? workers.map(worker => renderWorker(worker, task)).join('') : '<div class="empty">No workers are visible for this project. Enroll your own worker or ask an owner to approve this project.</div>';
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
      : 'Automatic mode · Validator demo uses simulation; approved API tasks use organization funding. Unavailable work queues.';
  renderArtifact();
  bindWorkerSettings();
  renderNativeProfiles();
  setControlsDisabled(busy);
  const writable = currentCanWrite();
  $('pause').disabled = busy || !task || !writable || !['running', 'queued'].includes(task.state);
  const sourceWorker = workers.find(worker => worker.id === task?.worker_id);
  $('resume').disabled = busy || !task || !writable || task.state !== 'handoff_pending' || sourceWorker?.capacity?.availability === 'quota_blocked' || !eligible(sourceWorker || {});
  $('handoffPhases').disabled = busy || !!task || !writable;
  $('demo').hidden = authMode !== 'demo';
  if (!writable) {
    ['switch', 'cancel', 'limit', 'continue'].forEach(id => $(id).disabled = true);
    document.querySelector('.send').disabled = true;
  }
}

function bindWorkerSettings() {
  document.querySelectorAll('[data-worker-form]').forEach(form => {
    form.onsubmit = event => {
      event.preventDefault();
      const worker = projectWorkers().find(item => item.id === form.dataset.workerForm);
      const visibleProjects = new Set(overview.projects.map(item => item.id));
      const unchangedHiddenProjects = (worker.permitted_project_ids || []).filter(id => !visibleProjects.has(id));
      action('workers/configure', {
        worker_id: form.dataset.workerForm,
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
  project = $('project').value;
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
    const created = await api('projects', {name, request_id: crypto.randomUUID()});
    project = created.id;
    loading = false;
    invalidateView();
    await load(true);
  } catch (failure) {
    showError(failure);
  } finally {
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
  if (!detail || busy) return;
  const version = viewVersion;
  const conversation = detail.conversation.id;
  $('more').disabled = true;
  try {
    const page = await api(`conversations/${encodeURIComponent(conversation)}/events?after=${historyAfter}&limit=100`);
    if (version !== viewVersion) return;
    historyEvents.push(...page.events);
    historyAfter = page.next_after;
    $('history').textContent = JSON.stringify(historyEvents, null, 2);
    $('more').textContent = page.events.length === 100 ? 'Load next 100 events' : 'Check for more events';
  } catch (failure) {
    if (version === viewVersion) showError(failure);
  } finally {
    $('more').disabled = false;
  }
};

function renderEnrollmentMode() {
  const mode = $('workerMode').value;
  const organizationOption = $('workerOwner').querySelector('option[value="org"]');
  organizationOption.disabled = overview?.user.role !== 'admin';
  $('workerOwner').disabled = ['organization_api', 'native'].includes(mode);
  $('nativeProfileLabel').hidden = mode !== 'native';
  $('nativeProfile').hidden = mode !== 'native';
  $('nativeProfile').disabled = mode !== 'native';
  if (mode === 'native') {
    $('workerOwner').value = 'user';
    $('enrollmentModeNote').textContent = 'Uses one independently authenticated local CODEX_HOME. Authenticate with scripts/native_profiles.py; only the configured local owner can initiate this profile. No Docker or API key required.';
  } else if (mode === 'organization_api') {
    $('workerOwner').value = 'org';
    $('enrollmentModeNote').textContent = 'Organization API workers require an administrator and server-side credential/runtime configuration. All workers under that organization share API quota.';
  } else {
    if (overview?.user.role !== 'admin' || $('workerOwner').value === 'org') $('workerOwner').value = 'user';
    $('enrollmentModeNote').textContent = mode === 'personal_supported'
      ? 'This enrollment remains disabled with unsupported_execution_mode. No provider-supported cross-user delegation mechanism has been verified for Relay.'
      : 'Runs a fixed, deterministic coding demo. Each simulated worker has its own simulated availability; no API key required.';
  }
}
$('enroll').onclick = () => {
  if (!overview) return;
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
  $('localNativeProfiles').innerHTML = nativeProfiles.profiles.map(profile => `<article class="native-profile"><strong>Codex ${esc(profile.profile.toUpperCase())}</strong><span class="pill">${esc(profile.status)}</span><p>${esc(profile.reason)}</p><button class="outline" data-native-profile="${esc(profile.profile)}" ${busy || loading || !detail || !selectedProject || !profile.available ? 'disabled' : ''}>${activeTask() ? 'Prepare ' + esc(profile.profile.toUpperCase()) + ' for handoff' : 'Select ' + esc(profile.profile.toUpperCase()) + ' for this project'}</button></article>`).join('');
  document.querySelectorAll('[data-native-profile]').forEach(button => button.onclick = async () => {
    if (busy || !selectedProject) return;
    busy = true;
    try {
      const worker = await api('native/profiles/enroll', {profile: button.dataset.nativeProfile, project_id: selectedProject});
      if (activeTask()) selectedTarget = worker.id;
      else selectedStartWorker = worker.id;
      await load(true);
      showNotice(activeTask() ? 'Native successor prepared. Use Request Handoff to authorize its execution explicitly.' : 'NATIVE LOCAL profile selected. Enter your coding objective; no simulation will be substituted.');
    } catch (failure) { showError(failure); }
    finally { busy = false; if (detail) render(); }
  });
}

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
  source.onmessage = () => {
    if (liveTimer) return;
    liveTimer = setTimeout(() => {
      liveTimer = null;
      if (!busy && !loading && lastConversation === conversation) load().catch(showError);
    }, 500);
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
  try { await api('logout', {}); showSignIn(); }
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
init().catch(showError);
setInterval(() => {
  const editingWorker = document.activeElement?.closest('.worker-settings');
  if (!document.body.classList.contains('signed-out') && !busy && !loading && !$('enrollmentDialog').open && !editingWorker && !document.querySelector('.worker-settings[open]')) load().catch(showError);
}, 5000);
