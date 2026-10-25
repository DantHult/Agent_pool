/* The editor/console presentation extends the existing authenticated Relay app. */
'use strict';
(function () {
  const byId = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  // One editor identity per tab. sessionStorage survives a reload but is copied
  // into a duplicated tab, so a tab that finds its identity held by another
  // live tab (Web Locks) takes a fresh one.
  let client = sessionStorage.getItem('relay-editor-client') || crypto.randomUUID();
  sessionStorage.setItem('relay-editor-client', client);
  const identity = (async () => {
    if (!navigator.locks || !window.AbortSignal?.timeout) return;
    const hold = id => new Promise(resolve => navigator.locks.request('relay-editor-client:' + id, {signal:AbortSignal.timeout(800)}, () => { resolve(true); return new Promise(() => {}); }).catch(() => resolve(false)));
    if (await hold(client)) return;
    client = crypto.randomUUID(); sessionStorage.setItem('relay-editor-client', client);
    await hold(client);
  })();
  const sessionLabel = (() => {
    const agent = navigator.userAgent;
    const browser = /Edg\//.test(agent) ? 'Edge' : /Firefox\//.test(agent) ? 'Firefox' : /Chrome\//.test(agent) ? 'Chrome' : /Safari\//.test(agent) ? 'Safari' : 'Browser';
    const system = /iPhone|iPad/.test(agent) ? 'iOS' : /Android/.test(agent) ? 'Android' : /Mac OS X/.test(agent) ? 'macOS' : /Windows/.test(agent) ? 'Windows' : /Linux/.test(agent) ? 'Linux' : '';
    return browser + (system ? ' on ' + system : '') + ' · tab opened ' + new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  })();
  const channel = 'BroadcastChannel' in window ? new BroadcastChannel('relay-editor') : null;
  const buffers = new Map();
  let bridge, project, user, conversation, state, current, editor, diffEditor;
  let opened = [], refreshAt = 0, refreshing = false, generation = 0, consoleSession = null, consoleTimer;
  let commandHistory = [], historyPosition = 0, paletteIndex = 0, activePane = 'explorer';
  let ready = false;
  let selectedEntry = null;
  let managementAvailable = false;
  let workflowAvailable = false, acquiring = null, transitioning = false, autoFailed = false, commandStarting = false;
  let refreshPromise = null;
  let consoleVersion = 0, treeSignature = null;
  let terminalEntries=[],activeEntry=null,lastTerminalResult=null,terminalAvailable=false;
  let leasesAvailable=false, handedOver=false, pendingHandover=null;
  const language = name => ({py:'python',js:'javascript',jsx:'javascript',mjs:'javascript',ts:'typescript',tsx:'typescript',html:'html',css:'css',scss:'css',json:'javascript',md:'markdown',yml:'yaml',yaml:'yaml',sh:'shell',sql:'sql',diff:'diff'}[name.split('.').pop()] || 'plaintext');
  // Time a program spent waiting for typed input is not its run time.
  const inputWait=ms=>ms>=100?' (' + (ms/1000).toFixed(1) + ' s of it waiting for input)':'';
  const bufferKey = name => [user, project, name].join(':');
  const mine = () => state?.lock?.kind === 'human' && state.lock.mine;
  const canReserve = () => project && state?.editor_writable!==false && bridge?.canWrite()!==false && (!state?.lock || mine() || state.lock.auto_recoverable);
  const context = () => ({project_id: project, client_id: client, conversation_id: conversation});
  const notifyError = error => bridge?.showError(error);
  const notice = text => bridge?.showNotice(text);
  const api = (path, body) => bridge.api(path, body);
  const guard = callback => async event => { try { byId('error').hidden=true;await callback(event); } catch (error) { notifyError(error); } };

  function pane(name) {
    activePane = name;
    document.querySelectorAll('[data-pane]').forEach(button => button.classList.toggle('active', button.dataset.pane === name));
    document.querySelectorAll('.sidebar-pane').forEach(panel => panel.hidden = panel.id !== 'pane-' + name);
    byId('sidebarTitle').textContent = {explorer:'EXPLORER',projects:'PROJECTS',agents:'AGENTS',conversations:'CONVERSATIONS',source:'SOURCE CONTROL'}[name];
    document.querySelector('.ide-shell').classList.add('sidebar-mobile-open');
    if (window.innerWidth<=950) document.querySelector('.ide-shell').classList.remove('agent-mobile-open');
  }
  document.querySelectorAll('[data-pane]').forEach(button => button.onclick = () => {
    const shell = document.querySelector('.ide-shell');
    if (window.innerWidth <= 650 && activePane === button.dataset.pane && shell.classList.contains('sidebar-mobile-open')) shell.classList.remove('sidebar-mobile-open');
    else pane(button.dataset.pane);
  });
  byId('showAgentSettings').onclick = () => pane('agents');
  byId('toggleAgent').onclick = () => {
    const shell = document.querySelector('.ide-shell');
    shell.classList.toggle(window.innerWidth <= 950 ? 'agent-mobile-open' : 'agent-hidden');
    editor?.layout(); diffEditor?.layout();
  };
  function bottom(name='console', show=true) {
    byId('bottomPanel').hidden = !show; byId('bottomResize').hidden = !show;
    byId('consolePane').hidden = name !== 'console'; byId('debugPane').hidden = name !== 'debug';
    document.querySelectorAll('[data-bottom]').forEach(button => button.classList.toggle('active', button.dataset.bottom === name));
    if (name === 'debug' && show) bridge?.loadHistory();
    editor?.layout(); diffEditor?.layout();
    if(name==='console' && show)focusTerminal();
  }
  document.querySelectorAll('[data-bottom]').forEach(button => button.onclick = () => bottom(button.dataset.bottom));
  byId('toggleTerminal').onclick = byId('toggleBottom').onclick = () => bottom('console', byId('bottomPanel').hidden);
  byId('closeBottom').onclick = () => bottom('console', false);
  function resizer(id, variable, horizontal, reverse, min, max) {
    const handle = byId(id);
    const change = delta => {
      const value = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(variable));
      const next = Math.max(min, Math.min(max(), value + delta));
      document.documentElement.style.setProperty(variable, next + 'px');
      editor?.layout(); diffEditor?.layout();
    };
    handle.addEventListener('pointerdown', event => {
      handle.setPointerCapture(event.pointerId);
      document.body.classList.add('resizing');
      let last = horizontal ? event.clientX : event.clientY;
      const move = next => { const position = horizontal ? next.clientX : next.clientY; change((position - last) * (reverse ? -1 : 1)); last = position; };
      const focused=document.activeElement;
      const end = () => { if(focused?.closest('#consolePane'))focused.focus();document.body.classList.remove('resizing'); handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', end); handle.removeEventListener('pointercancel', end); };
      handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', end); handle.addEventListener('pointercancel', end);
    });
    handle.addEventListener('keydown', event => { if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)) { event.preventDefault(); change((['ArrowRight','ArrowDown'].includes(event.key) ? 12 : -12) * (reverse ? -1 : 1)); } });
  }
  resizer('sidebarResize', '--sidebar-width', true, false, 180, () => Math.min(380, window.innerWidth * .32));
  resizer('agentResize', '--agent-width', true, true, 310, () => Math.min(700, window.innerWidth * .5));
  resizer('bottomResize', '--terminal-height', false, true, 150, () => window.innerHeight * .6);

  function ensureEditor() {
    if (!window.RelayMonaco || editor) return;
    const monaco = window.RelayMonaco;
    monaco.languages.register({id:'diff'});
    monaco.languages.setMonarchTokensProvider('diff',{tokenizer:{root:[[/^\+.*$/,'string'],[/^-.*$/,'invalid'],[/^@@.*$/,'keyword'],[/^diff.*$/,'comment']]}});
    monaco.editor.defineTheme('relay', {base:'vs-dark',inherit:true,rules:[{token:'comment',foreground:'718879'},{token:'keyword',foreground:'b59fdb'},{token:'string',foreground:'b9ce9b'},{token:'number',foreground:'d4b283'}],colors:{'editor.background':'#11161e','editor.foreground':'#d2dce7','editorLineNumber.foreground':'#435368','editorLineNumber.activeForeground':'#adc3bd','editorCursor.foreground':'#9ddab8','editor.selectionBackground':'#2a4a50','editor.lineHighlightBackground':'#18212c','editorIndentGuide.background1':'#26313e','editorWidget.background':'#1b2531','focusBorder':'#729c83'}});
    editor = monaco.editor.create(byId('monacoEditor'), {model:null,theme:'relay',fontFamily:'"SFMono-Regular",Consolas,"Liberation Mono",monospace',fontSize:13,lineHeight:22,minimap:{enabled:false},automaticLayout:true,scrollBeyondLastLine:false,padding:{top:15,bottom:15},readOnly:true,renderWhitespace:'selection',tabSize:4,wordWrap:'off',smoothScrolling:false,overviewRulerBorder:false,stickyScroll:{enabled:true},accessibilitySupport:'auto'});
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveCurrent().catch(notifyError));
    editor.addCommand(monaco.KeyCode.F5, () => {if(!byId('runFile').disabled)byId('runFile').click();});
    editor.onDidChangeCursorPosition(event => { byId('editorPosition').textContent = 'Ln ' + event.position.lineNumber + ', Col ' + event.position.column + '  ·  UTF-8'; });
    ready = true;
    for (const buffer of buffers.values()) makeModel(buffer);
    if (current) showBuffer(current);
  }
  window.addEventListener('relay-monaco-ready', ensureEditor);
  function makeModel(buffer) {
    if (!ready || buffer.model) return;
    const monaco = window.RelayMonaco;
    buffer.model = monaco.editor.createModel(buffer.text, language(buffer.path), monaco.Uri.parse('relay://' + buffer.project + '/' + encodeURIComponent(buffer.path) + '?view=' + encodeURIComponent(buffer.key)));
    buffer.listener = buffer.model.onDidChangeContent(() => {
      if (buffer.muting) return;
      buffer.text = buffer.model.getValue();
      buffer.dirty = buffer.text !== buffer.saved;
      renderTabs(); updateEditing(); scheduleDraft(buffer);
    });
  }
  function activeBuffer() { return current && buffers.get(current); }
  function showBuffer(key) {
    const buffer = buffers.get(key);
    if (!buffer) return;
    if (current && current !== key && editor) buffers.get(current).view = editor.saveViewState();
    current = key;
    byId('editorWelcome').hidden = true;
    byId('editorBreadcrumb').textContent = buffer.path.replaceAll('/', '  /  ');
    if (ready) {
      makeModel(buffer);
      byId('monacoEditor').hidden = false; byId('editorFallback').hidden = true;
      editor.setModel(buffer.model);
      if (buffer.view) editor.restoreViewState(buffer.view);
      editor.layout();
    } else {
      byId('editorFallback').hidden = false; byId('editorFallback').value = buffer.text;
    }
    renderTabs(); updateEditing(); renderTree();
    if (window.innerWidth<=950) document.querySelector('.ide-shell').classList.remove('agent-mobile-open');
    if (window.innerWidth <= 650) document.querySelector('.ide-shell').classList.remove('sidebar-mobile-open');
  }
  byId('editorFallback').addEventListener('input', () => {
    const buffer = activeBuffer(); if (!buffer) return;
    buffer.text = byId('editorFallback').value; buffer.dirty = buffer.text !== buffer.saved;
    renderTabs(); updateEditing(); scheduleDraft(buffer);
  });
  function replaceBuffer(buffer, text) {
    buffer.muting = true; buffer.text = text;
    if (buffer.model) buffer.model.setValue(text);
    buffer.muting = false;
    if (buffer.key === current) byId('editorFallback').value = text;
  }
  async function openFile(path, create=false) {
    const key = bufferKey(path), at = generation;
    if (buffers.has(key)) { if (!opened.includes(key)) opened.push(key); showBuffer(key); return; }
    byId('saveState').textContent = 'Opening…';
    const result = await api('ide/file?' + new URLSearchParams({project_id:project,path,create:create ? '1' : '0'}));
    if (at !== generation) return;
    const draft = state?.drafts?.find(item => item.path === path);
    const buffer = {key,project,path,version:draft ? draft.base_version : result.version,saved:result.content,text:draft?.content ?? result.content,syncedText:draft?.content,readonly:result.readonly,dirty:!!draft,model:null,view:null,pending:Promise.resolve(),timer:null};
    if (!buffer.readonly && canReserve() && !mine() && !transitioning) await acquire().catch(notifyError);
    if(at!==generation)return;
    buffers.set(key, buffer); opened.push(key); makeModel(buffer); showBuffer(key);
    sessionStorage.setItem('relay-open-files:' + user + ':' + project, JSON.stringify(opened.map(item => buffers.get(item).path)));
  }
  function virtualFile(title, text) {
    const key = bufferKey('virtual:' + title);
    let buffer = buffers.get(key);
    if (!buffer) { buffer = {key,project,path:title,version:null,saved:text,text,readonly:true,virtual:true,dirty:false,model:null}; buffers.set(key,buffer); }
    else { buffer.saved = text; replaceBuffer(buffer,text); }
    if (!opened.includes(key)) opened.push(key);
    makeModel(buffer); showBuffer(key);
  }
  function renderTabs() {
    byId('editorTabs').innerHTML = opened.map(key => { const buffer = buffers.get(key); return '<div class="editor-tab" role="tab" tabindex="0" aria-selected="' + (key === current) + '" data-buffer="' + escape(key) + '"><span class="file-icon ' + language(buffer.path) + '">' + (buffer.virtual ? '⑂' : language(buffer.path) === 'python' ? 'py' : '{ }') + '</span><span>' + escape(buffer.path.split('/').pop()) + '</span>' + (buffer.dirty ? '<span class="dirty-dot" aria-label="Unsaved draft">●</span>' : '') + '<button data-close-buffer="' + escape(key) + '" aria-label="Close ' + escape(buffer.path) + '">×</button></div>'; }).join('');
    byId('editorTabs').querySelectorAll('[data-buffer]').forEach(tab => { tab.onclick = event => { if (!event.target.closest('[data-close-buffer]')) showBuffer(tab.dataset.buffer); }; tab.onkeydown = event => { if (['Enter',' '].includes(event.key)) showBuffer(tab.dataset.buffer); }; });
    byId('editorTabs').querySelectorAll('[data-close-buffer]').forEach(button => button.onclick = () => {
      const key = button.dataset.closeBuffer, buffer = buffers.get(key);
      opened = opened.filter(item => item !== key);
      if (current === key) { current = null; if (opened.length) showBuffer(opened[opened.length-1]); else { byId('editorWelcome').hidden=false; byId('monacoEditor').hidden=true; byId('editorFallback').hidden=true; byId('editorBreadcrumb').textContent='Workspace'; } }
      if (buffer.dirty) notice('Draft retained. Reopen this file or recover it after a refresh.');
      renderTabs(); updateEditing();
      sessionStorage.setItem('relay-open-files:' + user + ':' + project, JSON.stringify(opened.map(item => buffers.get(item).path)));
      if(!opened.length)leaveProject().catch(notifyError);
    });
  }
  function scheduleDraft(buffer) {
    clearTimeout(buffer.timer);
    if (buffer.virtual || buffer.readonly || !mine()) return;
    buffer.timer = setTimeout(() => flushDraft(buffer).catch(notifyError), 220);
  }
  function flushDraft(buffer) {
    clearTimeout(buffer.timer);
    const payload = {project_id:buffer.project,client_id:client,path:buffer.path,version:buffer.version,content:buffer.text};
    const dirty=buffer.dirty;
    buffer.pending = (buffer.pending || Promise.resolve()).catch(() => {}).then(() => api(dirty ? 'ide/draft' : 'ide/discard', payload)).then(result=>{buffer.syncedText=payload.content;updateEditing();return result;});
    return buffer.pending;
  }
  async function saveCurrent() { const buffer = activeBuffer(); if (buffer) await save(buffer); }
  function save(buffer) {
    if (buffer.saving) return buffer.saving;
    buffer.saving=saveBuffer(buffer).finally(()=>{buffer.saving=null;});
    return buffer.saving;
  }
  async function saveBuffer(buffer) {
    if (!buffer.dirty || buffer.readonly || buffer.virtual) return;
    if (!mine()) await acquire();
    const target={project_id:buffer.project,client_id:client,...(project===buffer.project ? {conversation_id:conversation} : {})};
    clearTimeout(buffer.timer); await buffer.pending;
    const content = buffer.text;
    byId('saveState').textContent = 'Saving…';
    const result = await api('ide/save', {...target,path:buffer.path,version:buffer.version,content});
    buffer.saved = content; buffer.version = result.version; buffer.dirty = buffer.text !== content;
    if (buffer.dirty) scheduleDraft(buffer);
    renderTabs(); updateEditing(); await refresh(true);
  }
  async function acquire(recover=false) {
    if(acquiring && !recover)return acquiring;
    const at=generation;
    acquiring=(async()=>{
      const result=await api(workflowAvailable && !recover ? 'ide/prepare' : 'ide/lock', {...context(),recover,label:sessionLabel});
      if(at!==generation)throw new Error('Project changed while reserving the workspace. Return to that project to continue.');
      state=result;autoFailed=false;handedOver=false;renderState();
    })();
    try{await acquiring;}catch(error){autoFailed=true;throw error;}finally{acquiring=null;updateEditing();}
  }
  byId('recoverLock').onclick = guard(async () => {
    const lock=state?.lock;
    if(lock && !lock.stale && !confirm('Your other Relay session for this project is still active. Taking over makes that session read-only. Its saved drafts and Git state are kept, but text it has not synchronized yet stays only in that tab. Take over here?'))return;
    await acquire(true);
  });
  byId('requestHandover').onclick = guard(async () => {
    if(!channel)throw new Error('This browser cannot message other tabs. Switch to the tab that is editing, or take over here.');
    const at=generation;
    await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{pendingHandover=null;reject(new Error('No tab in this browser answered. If it was closed or is suspended, use Take over editing here.'));},4000);
      pendingHandover=result=>{clearTimeout(timer);pendingHandover=null;result.ok?resolve():reject(new Error(result.error || 'That tab could not hand over editing.'));};
      channel.postMessage({type:'handover',user,project,from:client});
    });
    if(at===generation)await acquire(true);
  });
  // Another tab of this browser asks for the project: give it up cooperatively.
  async function handOver(requester) {
    const reply={type:'handover-result',to:requester};
    try {
      if(consoleSession && !consoleSession.closed)throw new Error('A command session is open in the other tab. Stop or close it there first.');
      handedOver=true;
      await flushDrafts();
      await api('ide/detach',{project_id:project,client_id:client});
      reply.ok=true;
      notice('Editing moved to another tab. This tab is read-only until you edit here again.');
    } catch(error) { handedOver=false; reply.error=error.message; }
    channel.postMessage(reply);
    await refresh(true);
  }
  if(channel)channel.onmessage=event=>{
    const message=event.data || {};
    if(message.type==='handover' && message.user===user && message.project===project && mine())handOver(message.from).catch(notifyError);
    if(message.type==='handover-result' && message.to===client)pendingHandover?.(message);
  };
  // Leases: the holder renews while open; a closing tab gives the project up.
  async function heartbeat() {
    if(!leasesAvailable || !project || !mine() || handedOver)return;
    const at=generation, result=await api('ide/heartbeat',{project_id:project,client_id:client,label:sessionLabel});
    if(at!==generation || result.held)return;
    notice('Editing for this project moved to another session. Open files here are read-only; text not yet synchronized stays in this tab.');
    await refresh(true);
  }
  (function renew(){setTimeout(()=>heartbeat().catch(()=>{}).finally(renew),15000);})();
  window.addEventListener('pagehide',()=>{if(leasesAvailable && project && mine() && !(consoleSession && !consoleSession.closed))bridge?.beacon('ide/detach',{project_id:project,client_id:client});});
  window.addEventListener('pageshow',event=>{if(event.persisted && project)refresh(true).then(heartbeat).catch(()=>{});});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible' && project)heartbeat().then(()=>refresh(true)).catch(()=>{});});
  byId('saveFile').onclick = guard(saveCurrent);
  async function saveAll() {
    for(const buffer of buffers.values())if(buffer.project===project && buffer.dirty && !buffer.virtual)await save(buffer);
    for(const buffer of buffers.values())if(buffer.project===project && buffer.pending)await buffer.pending;
  }
  async function finishReservation() {
    if(!mine())return;
    const result=await api(workflowAvailable?'ide/finish':'ide/release',{...context(),checkpoint:true});
    if(result.released){clearTimeout(consoleTimer);consoleSession=null;sessionStorage.removeItem(consoleKey());state={...state,lock:null};}
    renderState();
  }
  async function prepareForAgent() {
    transitioning=true;updateEditing();
    try{
      if(acquiring)await acquiring;
      // A stale reservation from this account's closed tab is checkpointed too.
      if(state?.lock?.kind==='human' && state.lock.auto_recoverable)await acquire();
      await saveAll();await finishReservation();
    }
    catch(error){transitioning=false;updateEditing();bottom('console',!!consoleSession);throw error;}
  }
  async function finishTransition(){try{await refresh(true);}finally{transitioning=false;updateEditing();}}
  async function leaveProject(changingProject=false) {
    if(changingProject){transitioning=true;updateEditing();}
    try {
      await flushDrafts();
      if(!mine() || consoleSession && !consoleSession.closed || [...buffers.values()].some(buffer=>buffer.project===project && buffer.dirty))return;
      transitioning=true;updateEditing();await finishReservation();
    }catch(error){transitioning=false;updateEditing();throw error;}
    finally{if(!changingProject)transitioning=false;}
  }
  byId('viewWriter').onclick=()=>bridge?.openTask(state?.active_project_task);
  byId('discardDraft').onclick = guard(async () => {
    const buffer = activeBuffer(); if (!buffer?.dirty) return;
    if (!window.confirm('Discard the unsaved draft for ' + buffer.path + '? The saved project file stays intact.')) return;
    clearTimeout(buffer.timer); await buffer.pending;
    await api('ide/discard', {...context(),path:buffer.path});
    const result = await api('ide/file?' + new URLSearchParams({project_id:project,path:buffer.path,create:'1'}));
    replaceBuffer(buffer,result.content); buffer.version=result.version; buffer.saved=result.content; buffer.dirty=false;
    await refresh(true); renderTabs(); updateEditing();
  });
  byId('newFile').onclick = guard(async () => {
    const name = window.prompt('New file path, relative to this project'); if (!name?.trim()) return;
    if (!mine()) await acquire();
    await api('ide/save', {...context(),path:name.trim(),version:null,content:''});
    await refresh(true); await openFile(name.trim());
  });
  byId('newFolder').onclick = guard(()=>newFolder());
  async function newFolder(prefix='') {
    const name=window.prompt('New folder path; its parent must exist',prefix);if(!name?.trim())return;
    if(!mine())await acquire();
    await api('ide/files',{...context(),action:'mkdir',path:name.trim()});await refresh(true);
  }
  async function saveAs() {
    const buffer=activeBuffer();if(!buffer || buffer.virtual)return;
    const name=window.prompt('Save a copy as a new project file',buffer.path);if(!name?.trim())return;
    if(!mine())await acquire();
    const at=generation;
    await api('ide/save',{...context(),path:name.trim(),version:null,content:buffer.text});
    if(at!==generation)return;
    await refresh(true);await openFile(name.trim());
  }
  byId('saveAsFile').onclick=guard(saveAs);
  function forgetPaths(path) {
    for(const [key,buffer] of buffers) if(buffer.project===project && (buffer.path===path || buffer.path.startsWith(path+'/'))) {
      clearTimeout(buffer.timer);buffer.listener?.dispose();buffer.model?.dispose();buffers.delete(key);opened=opened.filter(value=>value!==key);
      if(current===key)current=null;
    }
    if(current)showBuffer(current);else if(opened.length)showBuffer(opened[opened.length-1]);else{byId('editorWelcome').hidden=false;byId('monacoEditor').hidden=true;byId('editorFallback').hidden=true;byId('editorBreadcrumb').textContent='Workspace';}
    sessionStorage.setItem('relay-open-files:'+user+':'+project,JSON.stringify(opened.map(key=>buffers.get(key).path)));
    renderTabs();updateEditing();
  }
  async function changeEntry(action,path=selectedEntry || activeBuffer()?.path) {
    if(!path)return;
    if([...buffers.values()].some(buffer=>buffer.project===project && buffer.dirty && (buffer.path===path || buffer.path.startsWith(path+'/'))))throw new Error('Save or discard affected drafts before renaming or deleting.');
    if(!mine())await acquire();
    const at=generation,ctx=context();
    const entry=await api('ide/entry?'+new URLSearchParams({project_id:project,path}));
    if(at!==generation)return;
    let target;
    if(action==='rename'){target=window.prompt('New path; destination must not exist',path)?.trim();if(!target)return;}
    else if(!window.confirm('Delete '+entry.kind+' '+path+(entry.kind==='folder'?' and its '+entry.entries+' entries':'')+'? Unsaved changes must be saved or discarded first.'))return;
    await api('ide/files',{...ctx,action,path,version:entry.version,target,confirmed:action==='delete'});
    if(at!==generation)return;
    forgetPaths(path);selectedEntry=target || null;await refresh(true);
    if(action==='rename' && entry.kind==='file')await openFile(target);
    notice(action==='rename'?'Moved '+path+' to '+target:'Deleted '+path);
  }
  byId('renameEntry').onclick=guard(()=>changeEntry('rename',activeBuffer()?.path));
  byId('deleteEntry').onclick=guard(()=>changeEntry('delete',activeBuffer()?.path));
  function updateRunControls() {
    const buffer=activeBuffer();
    byId('runFile').disabled=!terminalAvailable || !buffer || buffer.virtual || buffer.readonly || !runnable(buffer.path) || !state?.console_available || !canReserve() || commandStarting || consoleSession?.state==='running';
    byId('stopFile').disabled=consoleSession?.state!=='running';
    byId('runFile').title=!terminalAvailable?'Restart Relay to load the Run File backend':!buffer || !runnable(buffer.path)?'Run File supports .py, .js, .mjs and .cjs; use an approved project script for other types':!state?.console_available?(state?.console_unavailable_reason || 'The native command sandbox is unavailable'):'Run current file (F5). Pending edits are saved with conflict checks.';
  }
  byId('fileTree').addEventListener('contextmenu',event=>{
    const row=event.target.closest('[data-open-file], [data-folder] > summary');
    if(!row)return;event.preventDefault();
    selectedEntry=row.dataset.openFile || row.parentElement.dataset.folder;
    const menu=byId('fileContextMenu');byId('fileContextPath').textContent=selectedEntry;
    const folder=row.tagName==='SUMMARY';
    menu.querySelector('[data-file-action="open"]').disabled=folder;
    menu.querySelector('[data-file-action="run"]').disabled=!terminalAvailable || folder || !runnable(selectedEntry) || !state.console_available || !canReserve();
    menu.querySelectorAll('[data-file-action]:not([data-file-action="open"]):not([data-file-action="run"])').forEach(button=>button.disabled=!managementAvailable || state?.editor_writable===false || !bridge.canWrite() || state?.lock?.kind==='agent');
    menu.style.left=Math.min(event.clientX,window.innerWidth-250)+'px';menu.style.top=Math.min(event.clientY,window.innerHeight-230)+'px';menu.hidden=false;
    menu.querySelector('button:not(:disabled)')?.focus();
  });
  document.querySelectorAll('[data-file-action]').forEach(button=>button.onclick=guard(async()=>{
    byId('fileContextMenu').hidden=true;const action=button.dataset.fileAction,path=selectedEntry;
    if(action==='open')return openFile(path);
    if(action==='run')return executeCommand('', '', path);
    const prefix=(state.folders || []).includes(path)?path+'/':path.includes('/')?path.slice(0,path.lastIndexOf('/')+1):'';
    if(action==='new-folder')return newFolder(prefix);
    if(action==='new-file'){
      const name=window.prompt('New file path',prefix);if(!name?.trim())return;
      if(!mine())await acquire();await api('ide/save',{...context(),path:name.trim(),version:null,content:''});await refresh(true);await openFile(name.trim());return;
    }
    await changeEntry(action,path);
  }));
  document.addEventListener('click',event=>{if(!event.target.closest('#fileContextMenu'))byId('fileContextMenu').hidden=true;});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')byId('fileContextMenu').hidden=true;});

  function updateEditing() {
    const buffer = activeBuffer();
    const writable = state?.editor_writable!==false && mine() && !transitioning && !buffer?.readonly && !buffer?.virtual;
    editor?.updateOptions({readOnly:!writable});
    byId('editorFallback').readOnly = !writable;
    byId('saveFile').disabled = !writable || !buffer?.dirty;
    updateRunControls();
    byId('discardDraft').disabled = !mine() || !buffer?.dirty;
    byId('saveState').textContent = buffer?.dirty ? buffer.syncedText===buffer.text ? 'Draft retained' : 'Saving draft…' : buffer?.virtual ? 'Recorded view' : buffer?.readonly ? 'Protected file' : writable && buffer ? 'Saved' : acquiring ? 'Preparing workspace…' : state?.lock?.kind==='agent' ? 'Agent writing' : state?.lock && !mine() ? 'Another editor is active' : bridge?.canWrite()===false ? 'Project permission required' : state?.editor_writable===false ? 'Secure writes unavailable on this platform' : 'Ready';
    byId('recoverLock').hidden = !(state?.lock?.kind === 'human' && state.lock.recoverable && !state.lock.mine);
    byId('requestHandover').hidden = !(channel && state?.lock?.kind === 'human' && state.lock.recoverable && !state.lock.mine && state.lock.same_browser && !state.lock.stale);
    byId('newFile').disabled = !project || !canReserve() || transitioning;
    byId('newFolder').disabled=byId('newFile').disabled || !managementAvailable;
    byId('saveAsFile').disabled=byId('newFile').disabled || !buffer || buffer.virtual;
    byId('renameEntry').disabled=byId('deleteEntry').disabled=byId('newFile').disabled || !buffer || buffer.readonly || buffer.virtual || !managementAvailable;
    byId('unstageAll').disabled=!canReserve() || !managementAvailable || transitioning;
    for(const id of ['newFolder','renameEntry','deleteEntry','unstageAll'])if(!managementAvailable)byId(id).title='Restart Relay to enable this operation';
    byId('stageAll').disabled = byId('commitChanges').disabled = byId('createBranch').disabled = byId('switchBranch').disabled = !canReserve() || transitioning;
    if(buffer && !buffer.readonly && !buffer.virtual && canReserve() && !mine() && !transitioning && !acquiring && !autoFailed && !handedOver)acquire().catch(notifyError);
    if (state?.active_project_task && state.active_project_task.conversation_id!==conversation) document.querySelector('.send').disabled=true;
  }
  function treeFiles() {
    const files = [...(state?.files || [])];
    for (const draft of state?.drafts || []) if (!files.some(file => file.path === draft.path)) files.push({path:draft.path,size:draft.content.length,draft:true});
    for (const buffer of buffers.values()) if (buffer.project === project && buffer.dirty && !buffer.virtual && !files.some(file => file.path === buffer.path)) files.push({path:buffer.path,size:buffer.text.length,draft:true});
    return files;
  }
  function renderTree() {
    if (!state) return;
    const query = byId('fileFilter').value.toLowerCase();
    const signature=JSON.stringify([project,state.files,state.folders,state.changes,state.drafts?.map(d=>d.path),[...buffers.values()].filter(b=>b.project===project && b.dirty).map(b=>b.path),activeBuffer()?.path,query]);
    if(signature===treeSignature)return;treeSignature=signature;
    const root = {folders:new Map(),files:[]};
    for(const path of state.folders || []) {
      if(query && !path.toLowerCase().includes(query))continue;
      let node=root,prefix='';for(const part of path.split('/')){prefix+=(prefix?'/':'')+part;if(!node.folders.has(part))node.folders.set(part,{folders:new Map(),files:[],prefix});node=node.folders.get(part);}
    }
    for (const file of treeFiles().filter(file => file.path.toLowerCase().includes(query))) {
      const parts=file.path.split('/'); let node=root, prefix='';
      for (const part of parts.slice(0,-1)) { prefix += (prefix ? '/' : '') + part; if (!node.folders.has(part)) node.folders.set(part,{folders:new Map(),files:[],prefix}); node=node.folders.get(part); }
      node.files.push(file);
    }
    const expanded = new Map(Array.from(byId('fileTree').querySelectorAll('details'), node => [node.dataset.folder,node.open]));
    function html(node,depth) {
      return [...node.folders].sort(([a],[b])=>a.localeCompare(b)).map(([name,child])=>'<details class="tree-folder" data-folder="' + escape(child.prefix) + '" ' + (expanded.get(child.prefix) !== false ? 'open' : '') + '><summary style="padding-left:' + (8+depth*13) + 'px"><span class="folder-icon">▱</span>' + escape(name) + '</summary>' + html(child,depth+1) + '</details>').join('') + node.files.sort((a,b)=>a.path.localeCompare(b.path)).map(file=> {
        const change=state.changes.find(item=>item.path===file.path), dirty=buffers.get(bufferKey(file.path))?.dirty || file.draft;
        return '<button class="file-row ' + (activeBuffer()?.path === file.path ? 'selected' : '') + '" role="treeitem" data-open-file="' + escape(file.path) + '" style="padding-left:' + (12+depth*13) + 'px" title="' + escape(file.path) + '"><span class="file-icon ' + language(file.path) + '">' + (language(file.path)==='python' ? 'py' : language(file.path)==='javascript' ? 'JS' : file.path.endsWith('.md') ? 'M↓' : '◇') + '</span><span class="file-name">' + escape(file.path.split('/').pop()) + '</span><span class="file-change">' + (dirty ? '●' : escape(change?.status || '')) + '</span></button>';
      }).join('');
    }
    const markup = html(root,0) || '<div class="empty">No matching project files.</div>';
    if (byId('fileTree').innerHTML !== markup) byId('fileTree').innerHTML = markup;
    const drafts = [...buffers.values()].filter(buffer=>buffer.project===project && buffer.dirty).length || state.drafts.length;
    byId('fileTreeNote').textContent = state.files.length + ' files' + (drafts ? ' · ' + drafts + ' retained draft' + (drafts===1 ? '' : 's') : '');
  }
  document.addEventListener('click', event => { const button=event.target.closest('[data-open-file]'); if (button && project) openFile(button.dataset.openFile).catch(notifyError); });
  document.addEventListener('click',event=>{const button=event.target.closest('[data-artifact]');if(!button || !project)return;const at=generation;api('artifacts/'+encodeURIComponent(button.dataset.artifact)).then(record=>{if(at===generation)virtualFile('Recorded output',record.content);}).catch(notifyError);});
  byId('fileFilter').oninput = renderTree;
  const ago = seconds => seconds==null ? 'recently' : seconds<60 ? seconds + ' s ago' : Math.round(seconds/60) + ' min ago';
  function humanLockText(lock) {
    if(!('same_browser' in lock))return lock.name + ' is editing this project' + (lock.stale ? ' but has not been active recently' : '') + '. Their reservation stays until they save and finish.';
    const where=(lock.same_browser ? 'another tab in this browser' : 'another browser or device') + (lock.label ? ' (' + lock.label + ')' : '');
    if(lock.commands_active)return 'A command session is open for this project in ' + where + '. Stop or close it there before editing here.';
    if(lock.stale)return 'Your earlier session in ' + where + ' is no longer active (' + (lock.closed ? 'tab closed' : lock.session_ended ? 'signed out' : 'last seen ' + ago(lock.idle_seconds)) + '). ' + (lock.auto_recoverable ? 'Relay moves editing here as soon as you edit. Drafts and Git state are kept.' : 'Use Take over editing here to continue. Drafts and Git state are kept.');
    return 'You are editing this project in ' + where + ', active ' + ago(lock.idle_seconds) + '. ' + (lock.same_browser ? 'Switch to that tab, or ask it to hand over.' : 'Return to that session, or take over here.') + ' If it stops responding, it can be recovered after about ' + Math.ceil((lock.expires_in || 0)/60) + ' min.';
  }
  function renderState() {
    if (!state) return;
    renderTree(); updateEditing();
    byId('sourceBranch').textContent='⑂ ' + state.branch;
    byId('sourceChangeCount').textContent=state.changes.length + ' changes';
    byId('changeCount').textContent=state.changes.length; byId('changeCount').hidden=!state.changes.length;
    byId('statusBranch').textContent='⑂ ' + state.branch;
    const lock=state.lock;
    const otherTask=state.active_project_task && state.active_project_task.conversation_id!==conversation;
    const text=lock?.kind==='agent' ? 'An agent is writing this project. Wait for its checkpoint, or open its task and request Pause / Checkpoint to edit safely.' : lock?.kind==='human' ? (lock.mine ? 'Your edits are protected. Relay saves and releases this reservation automatically before an agent starts.' : humanLockText(lock)) : otherTask ? 'An unfinished task in another conversation owns this project. Finish or cancel it before starting another task.' : 'Ready. Editing and Git reserve this project automatically.';
    byId('workspaceLock').textContent=text; byId('workspaceLock').hidden=(!lock || mine()) && !otherTask;
    byId('viewWriter').hidden=lock?.kind!=='agent' && !otherTask;
    byId('workspaceLock').classList.toggle('agent-locked',lock?.kind==='agent');
    byId('sourceLock').textContent=text;
    byId('statusLock').textContent=lock?.kind==='agent' ? 'Agent writing' : mine() ? 'Editing' : lock ? 'Another editor is active' : 'Ready';
    byId('gitChanges').innerHTML=state.changes.length ? state.changes.map(change=>'<div class="git-change"><button data-open-file="' + escape(change.path) + '" title="' + escape(change.path) + '">' + escape(change.path) + '</button><span class="git-code">' + escape(change.status) + (change.staged ? ' ✓' : '') + '</span><button class="icon-button" data-stage="' + escape(change.path) + '" aria-label="Stage ' + escape(change.path) + '" ' + (!canReserve() ? 'disabled' : '') + '>＋</button></div>').join('') : '<div class="empty">Working tree is clean.</div>';
    byId('gitChanges').querySelectorAll('[data-stage]').forEach(button=>{const change=state.changes.find(value=>value.path===button.dataset.stage);if(change.staged){button.textContent='−';button.setAttribute('aria-label','Unstage '+change.path);if(!managementAvailable){button.disabled=true;button.title='Restart Relay to enable unstage';}}button.onclick=guard(()=>runGit([change.staged?'unstage':'add',button.dataset.stage]));});
    byId('consoleScope').textContent=state.console_available ? 'Private snapshot · approved commands · no host shell' : state.console_unavailable_reason || 'Command execution requires the authenticated local owner';
    const selectedCwd=byId('commandCwd').value;
    const folders=new Set(['']); for (const file of state.files) { const parts=file.path.split('/'); if (parts.length>1 && !parts[0].startsWith('.')) folders.add(parts.slice(0,-1).join('/')); }
    byId('commandCwd').innerHTML=[...folders].sort().map(folder=>'<option value="' + escape(folder) + '">' + escape(folder || 'Project root') + '</option>').join(''); byId('commandCwd').value=folders.has(selectedCwd) ? selectedCwd : '';
    renderConsole();
  }
  async function refresh(force=false) {
    if(refreshPromise)return refreshPromise;
    if (!project || (!force && Date.now()-refreshAt<3000)) return;
    refreshing=true; const at=generation, selected=project;
    const work=(async()=>{
      await identity;
      const next=await api('ide/state?' + new URLSearchParams({project_id:selected,client_id:client}));
      if (at!==generation) return;
      const previousHead=state?.checkpoint;
      const holder=lock=>JSON.stringify(lock && {...lock,idle_seconds:null,expires_in:null});
      if(holder(state?.lock)!==holder(next.lock))autoFailed=false;
      // A handover is complete once another editor holds the project.
      if(next.lock && !next.lock.mine)handedOver=false;
      state=next; refreshAt=Date.now(); renderState();
      if (previousHead && previousHead!==next.checkpoint) {
        for (const buffer of buffers.values()) if (buffer.project===selected && !buffer.dirty && !buffer.virtual) {
          try { const file=await api('ide/file?' + new URLSearchParams({project_id:selected,path:buffer.path})); if (!buffer.dirty) { replaceBuffer(buffer,file.content); buffer.saved=file.content; buffer.version=file.version; } } catch {}
        }
      }
    })();
    refreshPromise=work;
    try{await work;}finally{if(refreshPromise===work){refreshPromise=null;refreshing=false;}}
  }
  byId('refreshWorkspace').onclick=guard(()=>{autoFailed=false;return refresh(true);});
  async function runGit(arguments_) {
    if (arguments_[0]==='commit' && !arguments_[2]?.trim()) throw new Error('Enter a commit message.');
    if(['add','unstage','commit','branch','switch'].includes(arguments_[0]) && (arguments_[0]!=='branch' || arguments_.length>1)) {
      if(!mine())await acquire();await saveAll();
    }
    const at=generation;
    const result=await api('ide/git',{...context(),arguments:arguments_});
    if (at!==generation) return;
    bottom('console');
    const entry=terminalEntry('git '+arguments_.join(' '),'','authoritative project Git');entry.output=result.output || '(no output)';entry.exit=result.exit_code;renderTerminal();
    lastTerminalResult={kind:'git',...result};
    if (arguments_[0]==='diff' || arguments_[0]==='log') virtualFile(arguments_[0]==='diff' ? 'Working tree.diff' : 'Git history',result.output || 'No changes.');
    if (result.exit_code!==0) notice('Git exited with code ' + result.exit_code + '. Inspect its output.');
    else if (arguments_[0]==='commit') byId('commitMessage').value='';
    await refresh(true); await bridge.refresh();
  }
  document.querySelectorAll('[data-git]').forEach(button=>button.onclick=guard(()=>runGit([button.dataset.git])));
  byId('stageAll').onclick=guard(()=>runGit(['add','.']));
  byId('unstageAll').onclick=guard(()=>runGit(['unstage','.']));
  byId('stagedDiff').onclick=guard(()=>runGit(['diff','--staged']));
  async function reviewTask(task) {
    const at=generation;
    const result=await api('ide/review?'+new URLSearchParams({project_id:project,task_id:task}));
    if(at!==generation)return;
    virtualFile('Agent checkpoint changes.diff',result.attribution+'\n\n'+result.files.join('\n')+'\n\n'+(result.diff || 'No saved source changes.'));
  }
  byId('compareCheckpoints').onclick=guard(async()=>{
    const at=generation,result=await api('ide/review?'+new URLSearchParams({project_id:project,before:byId('compareBefore').value,after:byId('compareAfter').value}));
    if(at===generation)virtualFile('Checkpoint comparison.diff',result.diff || 'No saved source changes.');
  });
  byId('commitChanges').onclick=guard(()=>runGit(['commit','-m',byId('commitMessage').value]));
  byId('createBranch').onclick=guard(()=>runGit(['branch',byId('branchName').value]));
  byId('switchBranch').onclick=guard(()=>runGit(['switch',byId('branchName').value]));
  byId('openHandoff').onclick=guard(()=>openFile('.relay/HANDOFF.md'));

  function consoleKey() { return 'relay-console:' + user + ':' + project; }
  const terminalReady=()=>!!(state?.terminal_available || state?.console_available) && bridge?.canWrite()!==false;
  function renderConsole() {
    const item=consoleSession;
    byId('consoleState').textContent=commandStarting ? 'Preparing command…' : lastTerminalResult?.kind==='rejected' ? 'Command rejected' : lastTerminalResult?.kind==='git' ? 'finished · exit '+lastTerminalResult.exit_code+(lastTerminalResult.duration_ms!=null?' · '+lastTerminalResult.duration_ms+' ms':'') : item ? item.state + (item.exit_code!=null ? ' · exit ' + item.exit_code + ' · ' + (item.duration_ms/1000).toFixed(1) + 's' + inputWait(item.input_wait_ms) : '') : state?.console_available ? 'Ready · enter an approved command' : state?.terminal_available ? 'Ready · project Git available' : 'Command sandbox unavailable for this session';
    byId('closeConsole').disabled=!item || item.closed || commandStarting;
    // A failed preparation is re-prepared by the next command, so it must not
    // leave the prompt locked.
    const canRun=terminalReady() && !transitioning && !commandStarting && (!item || item.closed || ['ready','finished','failed'].includes(item.state));
    const running=item?.state==='running';
    const input=byId('commandInput');
    // Never readOnly: typing ahead is allowed while a command starts; submission is gated instead.
    input.disabled=!terminalReady();input.readOnly=false;
    byId('runCommand').disabled=!canRun && !running;
    byId('runCommand').textContent=running?'Send ↵':'Run ↵';
    byId('terminalPrompt').textContent=running?'stdin ›':'$';
    input.placeholder=input.disabled?(bridge?.canWrite()===false?'Project write permission is required to run commands':state?.console_unavailable_reason || 'Select a project to use the terminal'):running?'Type program input and press Enter · Ctrl+C stops execution':commandStarting || ['preparing','starting','reconnecting'].includes(item?.state)?'Preparing… you can type the next command now':'Type an approved command · Enter to run · ↑ ↓ history';
    byId('applyConsole').disabled=!item || item.closed || commandStarting || !['ready','finished'].includes(item.state);
    byId('cancelCommand').disabled=item?.state!=='running';
    byId('stdinForm').hidden=item?.state!=='running';
    byId('stopFile').disabled=item?.state!=='running';
    byId('terminalCwd').textContent='Project / '+((item?.state==='running'?activeEntry?.cwd:byId('commandCwd').value) || '.');
    updateRunControls();
  }
  function startConsolePoll() {
    clearTimeout(consoleTimer);
    const version=++consoleVersion;
    const poll=async()=>{
      if (version!==consoleVersion || !consoleSession?.id || consoleSession.closed) return;
      const identity=consoleSession.id, at=generation;
      try {
        const result=await api('ide/console?' + new URLSearchParams({session_id:identity}));
        if (version!==consoleVersion || at!==generation || consoleSession?.id!==identity) return;
        const changed=result.output!==consoleSession.output || result.command!==consoleSession.command;
        consoleSession=result;
        if (changed || !activeEntry || activeEntry.exit!==result.exit_code || activeEntry.duration!==result.duration_ms) {
          if(!activeEntry || activeEntry.session!==identity)activeEntry=terminalEntry(result.command || 'Restored command',byId('commandCwd').value,'isolated command workspace');
          activeEntry.session=identity;activeEntry.output=result.output || result.error || '';activeEntry.exit=result.exit_code;activeEntry.duration=result.duration_ms;activeEntry.inputWait=result.input_wait_ms;
          renderTerminal();
        }
        renderConsole();
      } catch (error) {
        if (version!==consoleVersion || at!==generation) return;
        consoleSession=null; sessionStorage.removeItem(consoleKey()); renderConsole();
        notice('The previous console is closed or no longer authorized. Saved project files and recorded command events are retained.');
        return;
      }
      consoleTimer=setTimeout(poll,consoleSession?.state==='running' ? 300 : 1200);
    };
    poll();
  }
  async function ensureConsole() {
    const started=generation,target=context();
    if(!mine())await acquire();
    await saveAll();
    if(started!==generation)throw new Error('Project changed before preparing its command environment.');
    if(!workflowAvailable && consoleSession && !consoleSession.closed && ['ready','finished'].includes(consoleSession.state))return;
    const at=generation,key=consoleKey();
    const result=await api(workflowAvailable?'ide/console/ensure':'ide/console/open',{...target,authorize:true});
    sessionStorage.setItem(key,result.id);
    if (at!==generation) throw new Error('Project changed while preparing its command environment.');
    consoleSession=result;
    byId('consoleState').textContent='Preparing isolated project environment…';
    renderConsole();
    const deadline=Date.now()+30000;
    while(consoleSession.state==='preparing' && Date.now()<deadline) {
      await new Promise(resolve=>setTimeout(resolve,150));
      const next=await api('ide/console?'+new URLSearchParams({session_id:result.id}));
      if(at!==generation)throw new Error('Project changed while preparing its command environment.');
      consoleSession=next;renderConsole();
    }
    if(!['ready','finished'].includes(consoleSession.state)){startConsolePoll();throw new Error(consoleSession.error || 'The command environment is still preparing. Wait briefly and try again.');}
  }
  const runnable=path=>/\.(py|js|mjs|cjs)$/i.test(path || '');
  function terminalEntry(command,cwd,scope) {
    const entry={command,cwd,scope,output:'',exit:null,offset:0};terminalEntries.push(entry);
    if(terminalEntries.length>100)terminalEntries.shift();
    return entry;
  }
  function renderTerminal(force=false) {
    const output=byId('consoleOutput'),follow=force || output.scrollHeight-output.clientHeight-output.scrollTop<80;
    let text=terminalEntries.map(entry=>'$ '+entry.command+'\n'+(entry.cwd?'cwd: '+entry.cwd+' · ':'')+(entry.scope || '')+'\n\n'+entry.output.slice(entry.offset || 0)+(entry.exit!=null?'\n\nExit '+entry.exit+(entry.duration!=null?' · '+entry.duration+' ms'+inputWait(entry.inputWait):''):'')).join('\n\n');
    if(text.length>1000000)text='[Older visible output omitted; recorded events remain available]\n'+text.slice(-1000000);
    output.textContent=text;if(follow)output.scrollTop=output.scrollHeight;
  }
  async function executeCommand(command,cwd,file=null) {
    if(commandStarting)return;
    if(!file && !command?.trim())return;
    const at=generation,target=context();
    commandStarting=true;lastTerminalResult=null;consoleVersion++;clearTimeout(consoleTimer);renderConsole();updateEditing();bottom('console');
    const entry=terminalEntry(file?'Run '+file:command,cwd,'Preparing approved command');activeEntry=entry;renderTerminal(true);
    try {
      await saveAll();
      const plan=terminalAvailable?await api(file?'ide/run-file':'ide/terminal',{...target,command,cwd,path:file}):file?(()=>{throw new Error('Restart Relay to enable Run File.');})():{kind:'sandbox',command,cwd};
      if(at!==generation)return;
      entry.command=plan.command;entry.cwd=plan.cwd;
      commandHistory.push(plan.command);historyPosition=commandHistory.length;if(!file && byId('commandInput').value.trim()===command.trim())byId('commandInput').value='';
      if(plan.kind==='git') {
        entry.scope='authoritative project Git';entry.output=plan.output || '(no output)';entry.exit=plan.exit_code;entry.duration=plan.duration_ms;lastTerminalResult=plan;
        await refresh(true);await bridge.refresh();
      }else {
        entry.scope='isolated command workspace';await ensureConsole();
        if(at!==generation)return;
        const identity=consoleSession.id;entry.session=identity;
        consoleSession={...consoleSession,state:'starting',exit_code:null};renderConsole();
        const result=await api('ide/console/run',{session_id:identity,command:plan.command,cwd:plan.cwd});
        if(at!==generation)return;consoleSession=result;startConsolePoll();
      }
      renderTerminal(true);
    }catch(error){entry.output='Command rejected: '+error.message;entry.scope='Execution error';lastTerminalResult={kind:'rejected'};renderTerminal(true);if(consoleSession && ['running','starting','preparing','reconnecting'].includes(consoleSession.state))startConsolePoll();throw error;}
    finally{commandStarting=false;renderConsole();updateEditing();}
  }
  function focusTerminal() {
    if(!byId('bottomPanel').hidden && !byId('consolePane').hidden && !byId('commandInput').disabled)byId('commandInput').focus({preventScroll:true});
  }
  // The output fills most of the terminal. Clicking its empty area must put
  // the caret at the prompt, while selection/copy of output stays native.
  byId('consoleOutput').addEventListener('pointerup',()=>{
    if(!window.getSelection()?.toString())focusTerminal();
  });
  byId('commandForm').addEventListener('click',event=>{if(event.target===byId('commandForm') || event.target===byId('terminalPrompt'))focusTerminal();});
  byId('commandForm').onsubmit=guard(async event=>{
    event.preventDefault();
    const input=byId('commandInput');
    if(consoleSession?.state==='running') {
      await api('ide/console/input',{session_id:consoleSession.id,input:input.value+'\n'});input.value='';focusTerminal();
    }else if(byId('runCommand').disabled) {
      if(input.value.trim())notice('The previous command is still starting; your command is kept in the prompt. Press Enter again when it finishes.');
    }else {await executeCommand(input.value.trim(),byId('commandCwd').value);focusTerminal();}
  });
  byId('runFile').onclick=guard(()=>executeCommand('', '', activeBuffer()?.path));
  byId('stopFile').onclick=guard(()=>api('ide/console/cancel',{session_id:consoleSession.id}));
  byId('clearTerminal').onclick=()=>{terminalEntries=[];activeEntry=null;if(consoleSession?.state==='running'){activeEntry=terminalEntry(consoleSession.command,'','Continuing command output');activeEntry.session=consoleSession.id;activeEntry.offset=consoleSession.output?.length || 0;}renderTerminal();};
  byId('copyTerminal').onclick=guard(async()=>{await navigator.clipboard.writeText(byId('consoleOutput').textContent);notice('Visible terminal output copied.');});
  byId('commandCwd').onchange=renderConsole;
  document.addEventListener('keydown',event=>{
    if(event.key==='F5' && activeBuffer() && !byId('runFile').disabled){event.preventDefault();byId('runFile').click();}
    const selected=event.target.selectionStart!=null && event.target.selectionStart!==event.target.selectionEnd;
    if(event.ctrlKey && !event.metaKey && event.key.toLowerCase()==='c' && event.target.closest('#consolePane') && !selected && !window.getSelection()?.toString()) {event.preventDefault();if(consoleSession?.state==='running')byId('cancelCommand').click();else byId('commandInput').value='';}
  });
  byId('commandInput').onkeydown=event=>{
    if (consoleSession?.state!=='running' && (event.key==='ArrowUp' || event.key==='ArrowDown')) { event.preventDefault(); historyPosition=Math.max(0,Math.min(commandHistory.length,historyPosition+(event.key==='ArrowUp' ? -1 : 1))); byId('commandInput').value=commandHistory[historyPosition] || ''; }
  };
  byId('cancelCommand').onclick=guard(()=>api('ide/console/cancel',{session_id:consoleSession.id}));
  byId('stdinForm').onsubmit=guard(async event=>{event.preventDefault();await api('ide/console/input',{session_id:consoleSession.id,input:byId('stdinInput').value+'\n'});byId('stdinInput').value='';});
  byId('closeStdin').onclick=guard(()=>api('ide/console/input',{session_id:consoleSession.id,close_stdin:true}));
  byId('closeConsole').onclick=guard(async()=>{const at=generation,key=consoleKey();await api('ide/console/close',{session_id:consoleSession.id});sessionStorage.removeItem(key);if(at!==generation)return;consoleSession=null;clearTimeout(consoleTimer);renderConsole();await refresh(true);});
  byId('applyConsole').onclick=guard(async()=>{
    if ([...buffers.values()].some(buffer=>buffer.project===project && buffer.dirty)) throw new Error('Save or discard editor drafts before applying console changes.');
    const at=generation,key=consoleKey();
    const result=await api('ide/console/apply',{session_id:consoleSession.id});
    sessionStorage.removeItem(key);if(at!==generation)return;
    consoleSession=null; clearTimeout(consoleTimer); renderConsole();
    notice('Console changes applied at checkpoint ' + result.checkpoint.slice(0,12) + '.');
    await refresh(true); await bridge.refresh();
  });

  function palette() {
    const query=byId('quickOpenInput').value.toLowerCase();
    const choices=treeFiles().filter(file=>file.path.toLowerCase().includes(query)).slice(0,30);
    paletteIndex=Math.min(Math.max(0,paletteIndex),Math.max(0,choices.length-1));
    byId('quickOpenResults').innerHTML=choices.map((file,index)=>'<button data-palette-file="' + escape(file.path) + '" class="' + (index===paletteIndex ? 'focused' : '') + '"><span class="file-icon">◇</span>' + escape(file.path) + '</button>').join('') || '<div class="empty">No matching project file.</div>';
    byId('quickOpenResults').querySelectorAll('button').forEach(button=>button.onclick=()=>{byId('quickOpenDialog').close();openFile(button.dataset.paletteFile).catch(notifyError);});
  }
  byId('quickOpen').onclick=()=>{if (!project) return;byId('quickOpenInput').value='';paletteIndex=0;palette();byId('quickOpenDialog').showModal();byId('quickOpenInput').focus();};
  byId('quickOpenInput').oninput=()=>{paletteIndex=0;palette();};
  byId('quickOpenInput').onkeydown=event=>{if (['ArrowUp','ArrowDown'].includes(event.key)) {event.preventDefault();paletteIndex+=event.key==='ArrowDown' ? 1 : -1;palette();} else if (event.key==='Enter') {event.preventDefault();byId('quickOpenResults').querySelector('.focused')?.click();}};
  document.addEventListener('keydown',event=>{
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase()==='p') {event.preventDefault();byId('quickOpen').click();}
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase()==='s') {event.preventDefault();saveCurrent().catch(notifyError);}
    if (event.ctrlKey && event.key==='`') {event.preventDefault();bottom('console',byId('bottomPanel').hidden);}
    if (event.key==='Escape') document.querySelector('.ide-shell').classList.remove('sidebar-mobile-open','agent-mobile-open');
  });
  window.addEventListener('beforeunload',event=>{if ([...buffers.values()].some(buffer=>buffer.dirty && buffer.syncedText!==buffer.text)) {event.preventDefault();event.returnValue='Wait for your draft to finish synchronizing.';}});
  function signOut() {
    generation++; clearTimeout(consoleTimer); project=null;state=null;current=null;opened=[];consoleSession=null;refreshAt=0;transitioning=false;autoFailed=false;acquiring=null;commandStarting=false;refreshPromise=null;
    for (const buffer of buffers.values()) {clearTimeout(buffer.timer);buffer.listener?.dispose();buffer.model?.dispose();}
    buffers.clear();editor?.setModel(null);renderTabs();
    byId('editorWelcome').hidden=false;byId('monacoEditor').hidden=true;byId('editorFallback').hidden=true;byId('editorFallback').value='';byId('consoleOutput').textContent='Sign in to access your project console.';byId('fileTree').replaceChildren();
    byId('gitChanges').replaceChildren();byId('sourceBranch').textContent='—';byId('statusBranch').textContent='⑂ —';byId('workspaceLock').hidden=true;
  }
  async function update(value) {
    if (!bridge) return;
    managementAvailable=value.managementAvailable===true;
    workflowAvailable=value.automaticWorkflow===true;terminalAvailable=value.developmentTerminal===true;leasesAvailable=value.editorLeases===true;
    const changed=project!==value.project || user!==value.user;
    if (changed) {
      generation++;clearTimeout(consoleTimer);project=value.project;user=value.user;state=null;refreshAt=0;opened=[];current=null;consoleSession=null;refreshing=false;autoFailed=false;acquiring=null;transitioning=false;refreshPromise=null;
      byId('editorWelcome').hidden=false;byId('monacoEditor').hidden=true;byId('editorFallback').hidden=true;renderTabs();
      commandHistory=[];historyPosition=0;terminalEntries=[];activeEntry=null;lastTerminalResult=null;
    }
    conversation=value.conversation;
    byId('statusProject').textContent=value.projectName;
    byId('statusWorker').textContent=value.worker || 'No worker';
    byId('statusExecution').textContent=value.state || 'Ready';
    byId('activeWorkerLabel').textContent=value.worker || 'No worker selected';
    byId('taskObjective').textContent=value.objective || 'Start a conversation with your agent';
    byId('taskObjectiveFull').textContent=value.objective || '';
    byId('latestCheckpoint').textContent=value.checkpoint?.slice(0,8) || '—';
    byId('selectedWorkerLabel').textContent=value.selectedWorker || 'Select worker';
    const checkpoints=value.checkpoints || [];
    for(const id of ['compareBefore','compareAfter']) {
      const selected=byId(id).value;
      byId(id).innerHTML=checkpoints.map(item=>'<option value="'+escape(item.git_commit)+'">'+escape(item.git_commit.slice(0,9)+' · '+item.reason)+'</option>').join('');
      if(checkpoints.some(item=>item.git_commit===selected))byId(id).value=selected;
      else if(id==='compareBefore' && checkpoints.length>1)byId(id).value=checkpoints[1].git_commit;
    }
    byId('compareCheckpoints').disabled=checkpoints.length<2 || !managementAvailable;
    byId('compareCheckpoints').title=checkpoints.length<2?'Save at least two project checkpoints to compare their changes':!managementAvailable?'Restart Relay to provide checkpoint review':'Compare the selected recorded checkpoints';
    await refresh(changed);
    updateEditing();
    if (changed && project) {
      heartbeat().catch(()=>{});
      let restore=[];try {restore=JSON.parse(sessionStorage.getItem('relay-open-files:' + user + ':' + project) || '[]');} catch {}
      for (const path of restore.slice(0,12)) await openFile(path).catch(()=>{});
      for (const draft of state?.drafts || []) await openFile(draft.path,true).catch(()=>{});
      const identity=sessionStorage.getItem(consoleKey());
      if (identity) {consoleSession={id:identity,state:'reconnecting'};bottom('console');renderConsole();startConsolePoll();}
    }
    ensureEditor();
  }
  byId('newConversation').onclick=()=>bridge?.newConversation();
  byId('showCheckpoint').onclick=()=>{pane('agents');document.querySelector('.workspace-records').open=true;document.querySelector('[data-tab="checkpoints"]').click();};
  async function flushDrafts(){for(const buffer of buffers.values())if(buffer.dirty && !buffer.virtual && !buffer.readonly)await flushDraft(buffer);}
  window.RelayIDE={connect(value){bridge=value;ensureEditor();},update,openFile,virtualFile,refresh,pane,bottom,signOut,updateEditing,flushDrafts,reviewTask,prepareForAgent,finishTransition,leaveProject,
    getState:()=>state,get clientId(){return client;},get editor(){return editor;},get activeFile(){return activeBuffer()?.path;}};
})();
