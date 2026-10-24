/* The editor/console presentation extends the existing authenticated Relay app. */
'use strict';
(function () {
  const byId = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const client = sessionStorage.getItem('relay-editor-client') || crypto.randomUUID();
  sessionStorage.setItem('relay-editor-client', client);
  const buffers = new Map();
  let bridge, project, user, conversation, state, current, editor, diffEditor;
  let opened = [], refreshAt = 0, refreshing = false, generation = 0, consoleSession = null, consoleTimer;
  let commandHistory = [], historyPosition = 0, paletteIndex = 0, activePane = 'explorer';
  let ready = false;
  const language = name => ({py:'python',js:'javascript',jsx:'javascript',mjs:'javascript',ts:'typescript',tsx:'typescript',html:'html',css:'css',scss:'css',json:'javascript',md:'markdown',yml:'yaml',yaml:'yaml',sh:'shell',sql:'sql',diff:'diff'}[name.split('.').pop()] || 'plaintext');
  const bufferKey = name => [user, project, name].join(':');
  const mine = () => state?.lock?.kind === 'human' && state.lock.mine;
  const context = () => ({project_id: project, client_id: client, conversation_id: conversation});
  const notifyError = error => bridge?.showError(error);
  const notice = text => bridge?.showNotice(text);
  const api = (path, body) => bridge.api(path, body);
  const guard = callback => async event => { try { byId('error').hidden=true;await callback(event); } catch (error) { notifyError(error); } };

  function pane(name) {
    activePane = name;
    document.querySelectorAll('[data-pane]').forEach(button => button.classList.toggle('active', button.dataset.pane === name));
    document.querySelectorAll('.sidebar-pane').forEach(panel => panel.hidden = panel.id !== 'pane-' + name);
    byId('sidebarTitle').textContent = {explorer:'EXPLORER',agents:'AGENTS',conversations:'CONVERSATIONS',source:'SOURCE CONTROL'}[name];
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
      const end = () => { document.body.classList.remove('resizing'); handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', end); handle.removeEventListener('pointercancel', end); };
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
    if (!mine()) throw new Error('Acquire or recover your edit lock before saving this draft.');
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
    const at=generation;
    const result=await api('ide/lock', {...context(),recover});
    if (at!==generation) throw new Error('Project changed while the edit lock was being acquired. Return to that project to continue.');
    state=result;renderState();
  }
  byId('editLock').onclick = guard(() => acquire());
  byId('recoverLock').onclick = guard(() => acquire(true));
  byId('saveFile').onclick = guard(saveCurrent);
  byId('releaseLock').onclick = guard(async () => {
    const target=context();
    for (const buffer of buffers.values()) if (buffer.project === target.project_id && buffer.dirty) await save(buffer);
    const result = await api('ide/release', {...target,checkpoint:true});
    notice('Checkpoint ' + result.checkpoint.slice(0,12) + ' saved. Workspace released for agents.');
    await refresh(true); await bridge.refresh();
  });
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
    await openFile(name.trim(),true);
  });

  function updateEditing() {
    const buffer = activeBuffer();
    const writable = state?.editor_writable!==false && mine() && !buffer?.readonly && !buffer?.virtual;
    editor?.updateOptions({readOnly:!writable});
    byId('editorFallback').readOnly = !writable;
    byId('saveFile').disabled = !writable || !buffer?.dirty;
    byId('discardDraft').disabled = !mine() || !buffer?.dirty;
    byId('saveState').textContent = buffer?.dirty ? buffer.syncedText===buffer.text ? 'Draft retained' : 'Saving draft…' : writable && buffer ? 'Saved' : 'Read-only';
    byId('editLock').hidden = mine(); byId('editLock').disabled = !project || state?.editor_writable===false || state?.lock?.kind === 'agent' || bridge?.canWrite() === false;
    byId('editLock').textContent=state?.editor_writable===false ? 'Read-only platform' : 'Acquire edit lock';
    byId('releaseLock').hidden = !mine();
    byId('recoverLock').hidden = !(state?.lock?.kind === 'human' && state.lock.recoverable && !state.lock.mine);
    byId('newFile').disabled = state?.editor_writable===false || bridge?.canWrite() === false || state?.lock?.kind === 'agent';
    byId('stageAll').disabled = byId('commitChanges').disabled = byId('createBranch').disabled = byId('switchBranch').disabled = !mine();
    if (state?.lock?.kind === 'human') {
      ['switch','resume'].forEach(id => byId(id).disabled = true);
      document.querySelector('.send').disabled = true;
    }
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
    const root = {folders:new Map(),files:[]};
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
  function renderState() {
    if (!state) return;
    renderTree(); updateEditing();
    byId('sourceBranch').textContent='⑂ ' + state.branch;
    byId('sourceChangeCount').textContent=state.changes.length + ' changes';
    byId('changeCount').textContent=state.changes.length; byId('changeCount').hidden=!state.changes.length;
    byId('statusBranch').textContent='⑂ ' + state.branch;
    const lock=state.lock;
    const otherTask=state.active_project_task && state.active_project_task.conversation_id!==conversation;
    const text=lock?.kind==='agent' ? 'Agent owns the workspace. Files are read-only until its checkpoint is complete.' : lock?.kind==='human' ? (lock.mine ? 'Your edit lock is active. Agents are blocked until you checkpoint and release.' : lock.name + ' holds the edit lock. Saved files remain readable.') : otherTask ? 'An unfinished task in another conversation owns this project. Finish or cancel it before starting another task.' : 'No writer active. Acquire an edit lock to make changes.';
    byId('workspaceLock').textContent=text; byId('workspaceLock').hidden=!lock && !otherTask;
    byId('workspaceLock').classList.toggle('agent-locked',lock?.kind==='agent');
    byId('sourceLock').textContent=text;
    byId('statusLock').textContent=lock?.kind==='agent' ? 'Agent lock' : mine() ? 'Your edit lock' : lock ? 'Editor reserved' : 'Read-only';
    byId('gitChanges').innerHTML=state.changes.length ? state.changes.map(change=>'<div class="git-change"><button data-open-file="' + escape(change.path) + '" title="' + escape(change.path) + '">' + escape(change.path) + '</button><span class="git-code">' + escape(change.status) + (change.staged ? ' ✓' : '') + '</span><button class="icon-button" data-stage="' + escape(change.path) + '" aria-label="Stage ' + escape(change.path) + '" ' + (!mine() ? 'disabled' : '') + '>＋</button></div>').join('') : '<div class="empty">Working tree is clean.</div>';
    byId('gitChanges').querySelectorAll('[data-stage]').forEach(button=>button.onclick=guard(()=>runGit(['add',button.dataset.stage])));
    byId('enableConsole').disabled=!state.console_available || !mine();
    byId('consoleScope').textContent=state.console_available ? 'Private snapshot · approved commands · no host shell' : 'Command execution requires the authenticated local owner';
    const selectedCwd=byId('commandCwd').value;
    const folders=new Set(['']); for (const file of state.files) { const parts=file.path.split('/'); if (parts.length>1 && !parts[0].startsWith('.')) folders.add(parts.slice(0,-1).join('/')); }
    byId('commandCwd').innerHTML=[...folders].sort().map(folder=>'<option value="' + escape(folder) + '">' + escape(folder || 'Project root') + '</option>').join(''); byId('commandCwd').value=folders.has(selectedCwd) ? selectedCwd : '';
    renderConsole();
  }
  async function refresh(force=false) {
    if (!project || refreshing || (!force && Date.now()-refreshAt<3000)) return;
    refreshing=true; const at=generation, selected=project;
    try {
      const next=await api('ide/state?' + new URLSearchParams({project_id:selected,client_id:client}));
      if (at!==generation) return;
      const previousHead=state?.checkpoint;
      state=next; refreshAt=Date.now(); renderState();
      if (previousHead && previousHead!==next.checkpoint) {
        for (const buffer of buffers.values()) if (buffer.project===selected && !buffer.dirty && !buffer.virtual) {
          try { const file=await api('ide/file?' + new URLSearchParams({project_id:selected,path:buffer.path})); if (!buffer.dirty) { replaceBuffer(buffer,file.content); buffer.saved=file.content; buffer.version=file.version; } } catch {}
        }
      }
    } finally { refreshing=false; }
  }
  byId('refreshWorkspace').onclick=guard(()=>refresh(true));
  async function runGit(arguments_) {
    if (arguments_[0]==='commit' && !arguments_[2]?.trim()) throw new Error('Enter a commit message.');
    const at=generation;
    const result=await api('ide/git',{...context(),arguments:arguments_});
    if (at!==generation) return;
    bottom('console');
    byId('consoleOutput').textContent='$ git ' + arguments_.join(' ') + '\n\n' + (result.output || '(no output)') + '\n\nExit ' + result.exit_code;
    if (arguments_[0]==='diff' || arguments_[0]==='log') virtualFile(arguments_[0]==='diff' ? 'Working tree.diff' : 'Git history',result.output || 'No changes.');
    if (result.exit_code!==0) notice('Git exited with code ' + result.exit_code + '. Inspect its output.');
    else if (arguments_[0]==='commit') byId('commitMessage').value='';
    await refresh(true); await bridge.refresh();
  }
  document.querySelectorAll('[data-git]').forEach(button=>button.onclick=guard(()=>runGit([button.dataset.git])));
  byId('stageAll').onclick=guard(()=>runGit(['add','.']));
  byId('commitChanges').onclick=guard(()=>runGit(['commit','-m',byId('commitMessage').value]));
  byId('createBranch').onclick=guard(()=>runGit(['branch',byId('branchName').value]));
  byId('switchBranch').onclick=guard(()=>runGit(['switch',byId('branchName').value]));
  byId('openHandoff').onclick=guard(()=>openFile('.relay/HANDOFF.md'));

  function consoleKey() { return 'relay-console:' + user + ':' + project; }
  function renderConsole() {
    const item=consoleSession;
    byId('consoleState').textContent=item ? item.state + (item.exit_code!=null ? ' · exit ' + item.exit_code + ' · ' + (item.duration_ms/1000).toFixed(1) + 's' : '') : 'Not authorized';
    byId('enableConsole').hidden=!!item && !item.closed;
    byId('closeConsole').disabled=!item || item.closed;
    const canRun=item && !item.closed && ['ready','finished'].includes(item.state);
    byId('commandInput').disabled=byId('runCommand').disabled=!canRun;
    byId('applyConsole').disabled=!canRun;
    byId('cancelCommand').disabled=item?.state!=='running';
    byId('stdinForm').hidden=item?.state!=='running';
  }
  function startConsolePoll() {
    clearTimeout(consoleTimer);
    const poll=async()=>{
      if (!consoleSession?.id || consoleSession.closed) return;
      const identity=consoleSession.id, at=generation;
      try {
        const result=await api('ide/console?' + new URLSearchParams({session_id:identity}));
        if (at!==generation || consoleSession?.id!==identity) return;
        const changed=result.output!==consoleSession.output || result.command!==consoleSession.command;
        consoleSession=result;
        if (changed) {
          const output=byId('consoleOutput'), follow=output.scrollHeight-output.clientHeight-output.scrollTop<80;
          output.textContent=(result.command ? '$ ' + result.command + '\n\n' : '') + (result.output || result.error || 'Isolated environment ready. Enter an approved development command.');
          if (follow) output.scrollTop=output.scrollHeight;
        }
        renderConsole();
      } catch (error) {
        if (at!==generation) return;
        consoleSession=null; sessionStorage.removeItem(consoleKey()); renderConsole();
        notice('The previous console is closed or no longer authorized. Saved project files and recorded command events are retained.');
        return;
      }
      consoleTimer=setTimeout(poll,consoleSession?.state==='running' ? 300 : 1200);
    };
    poll();
  }
  byId('enableConsole').onclick=guard(async()=>{
    if (!mine()) throw new Error('Acquire the project edit lock before authorizing a console.');
    if ([...buffers.values()].some(buffer=>buffer.project===project && buffer.dirty)) throw new Error('Save or discard your drafts before opening the console snapshot.');
    const at=generation,key=consoleKey();
    const result=await api('ide/console/open',{...context(),authorize:true});
    sessionStorage.setItem(key,result.id);
    if (at!==generation) return;
    consoleSession=result;
    byId('consoleOutput').textContent='Preparing a private project snapshot and approved runtime…';
    renderConsole(); startConsolePoll();
  });
  byId('commandForm').onsubmit=guard(async event=>{
    event.preventDefault(); if (!consoleSession) return;
    const command=byId('commandInput').value.trim(); if (!command) return;
    const identity=consoleSession.id;
    const at=generation;
    consoleSession={...consoleSession,state:'starting',exit_code:null};renderConsole();
    try {const result=await api('ide/console/run',{session_id:identity,command,cwd:byId('commandCwd').value});if(at!==generation)return;consoleSession=result;}
    catch(error){startConsolePoll();throw error;}
    commandHistory.push(command); historyPosition=commandHistory.length; byId('commandInput').value='';
    byId('consoleOutput').textContent='$ ' + command + '\n'; renderConsole(); startConsolePoll();
  });
  byId('commandInput').onkeydown=event=>{
    if (event.key==='ArrowUp' || event.key==='ArrowDown') { event.preventDefault(); historyPosition=Math.max(0,Math.min(commandHistory.length,historyPosition+(event.key==='ArrowUp' ? -1 : 1))); byId('commandInput').value=commandHistory[historyPosition] || ''; }
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
    generation++; clearTimeout(consoleTimer); project=null;state=null;current=null;opened=[];consoleSession=null;refreshAt=0;
    for (const buffer of buffers.values()) {clearTimeout(buffer.timer);buffer.listener?.dispose();buffer.model?.dispose();}
    buffers.clear();editor?.setModel(null);renderTabs();
    byId('editorWelcome').hidden=false;byId('monacoEditor').hidden=true;byId('editorFallback').hidden=true;byId('editorFallback').value='';byId('consoleOutput').textContent='Sign in to access your project console.';byId('fileTree').replaceChildren();
    byId('gitChanges').replaceChildren();byId('sourceBranch').textContent='—';byId('statusBranch').textContent='⑂ —';byId('workspaceLock').hidden=true;
  }
  async function update(value) {
    if (!bridge) return;
    const changed=project!==value.project || user!==value.user;
    if (changed) {
      generation++;clearTimeout(consoleTimer);project=value.project;user=value.user;state=null;refreshAt=0;opened=[];current=null;consoleSession=null;refreshing=false;
      byId('editorWelcome').hidden=false;byId('monacoEditor').hidden=true;byId('editorFallback').hidden=true;renderTabs();
      commandHistory=[];historyPosition=0;
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
    await refresh(changed);
    updateEditing();
    if (changed && project) {
      let restore=[];try {restore=JSON.parse(sessionStorage.getItem('relay-open-files:' + user + ':' + project) || '[]');} catch {}
      for (const path of restore.slice(0,12)) await openFile(path).catch(()=>{});
      for (const draft of state?.drafts || []) await openFile(draft.path,true).catch(()=>{});
      const identity=sessionStorage.getItem(consoleKey());
      if (identity) {consoleSession={id:identity,state:'reconnecting'};renderConsole();startConsolePoll();}
    }
    ensureEditor();
  }
  byId('newConversation').onclick=()=>bridge?.newConversation();
  byId('showCheckpoint').onclick=()=>{pane('agents');document.querySelector('.workspace-records').open=true;document.querySelector('[data-tab="checkpoints"]').click();};
  async function flushDrafts(){for(const buffer of buffers.values())if(buffer.dirty && !buffer.virtual && !buffer.readonly)await flushDraft(buffer);}
  window.RelayIDE={connect(value){bridge=value;ensureEditor();},update,openFile,virtualFile,refresh,pane,bottom,signOut,updateEditing,flushDrafts,
    getState:()=>state,get clientId(){return client;},get editor(){return editor;},get activeFile(){return activeBuffer()?.path;}};
})();
