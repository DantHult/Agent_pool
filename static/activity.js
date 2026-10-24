/* Visible event projection. Raw events remain unchanged and available in Debug. */
'use strict';
(function (root) {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const metadata = event => { try { return typeof event.metadata === 'string' ? JSON.parse(event.metadata) : event.metadata || {}; } catch { return {}; } };
  function response(text) {
    const value = String(text || '').trim();
    try {
      const parsed = JSON.parse(value.replace(/^```(?:json)?\s*|\s*```$/g, ''));
      if (parsed && typeof parsed.summary === 'string') {
        let result = parsed.summary;
        for (const [key, title] of [['completed_work','Completed'], ['test_results','Tests'], ['pending_steps','Next steps'], ['limitations','Limitations']]) {
          if (Array.isArray(parsed[key]) && parsed[key].length) result += '\n\n**' + title + '**\n' + parsed[key].map(item => {const text=typeof item === 'string' ? item : JSON.stringify(item);return key==='test_results' && text.includes('\n') ? '\n```text\n'+text+'\n```' : '- '+text;}).join('\n');
        }
        return result;
      }
    } catch {}
    if (value.startsWith('{')) {
      const summary = value.match(/"summary"\s*:\s*"((?:\\.|[^"\\])*)/);
      if (summary) { try { return JSON.parse('"' + summary[1] + '"'); } catch {} }
      return 'Preparing response…';
    }
    return text;
  }
  function markdown(text) {
    // Input is escaped before any formatting. Source HTML is never executed.
    const pieces = String(text || '').split(/```([^\n`]*)\n([\s\S]*?)(?:```|$)/g);
    return pieces.map((part, index) => {
      if (index % 3 === 1) return '';
      if (index % 3 === 2) return '<div class="message-code"><span>' + escape(pieces[index-1] || 'code') + '</span><pre><code>' + escape(part.trimEnd()) + '</code></pre></div>';
      return escape(part).replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
        .split('\n\n').filter(Boolean).map(paragraph => '<p>' + paragraph.replace(/^#{1,4}\s+(.+)$/gm, '<strong>$1</strong>').replace(/^- /gm, '• ').replace(/\n/g, '<br>') + '</p>').join('');
    }).join('');
  }
  class Projection {
    constructor() { this.groups = []; this.items = new Map(); this.seen = new Set(); this.sessions = new Map(); this.operations = new Map(); this.serial = 0; }
    append(input) {
      for (const event of input) {
        const identity = event.event_id || event.conversation_id + ':' + event.sequence;
        if (this.seen.has(identity)) continue;
        this.seen.add(identity);
        const meta = metadata(event);
        const run = event.task_id + ':' + (event.worker_id || 'human');
        if (event.event_type === 'session_started' || event.event_type === 'assignment') this.sessions.set(run, event.sequence);
        const base = run + ':' + (this.sessions.get(run) || 'initial');
        const item = meta.provider_item_id;
        const type = event.event_type;
        if (/reasoning/i.test(type)) continue;
        let key, group;
        if (type === 'agent_delta' || type === 'response') {
          if (type==='response' && !item && meta.origin==='relay') {
            const previous=this.groups.slice().reverse().find(entry=>entry.kind==='assistant' && entry.complete && entry.key.startsWith(base+':message:'));
            if (previous) {
              let summary=previous.text;try {summary=JSON.parse(previous.text).summary || summary;} catch {}
              if (String(summary).trim()===String(event.content).trim()) {previous.updated=event.sequence;continue;}
            }
          }
          key = base + ':message:' + (item || this.serial);
          group = this.items.get(key);
          if (!group) { group = this.add(key, 'assistant', event, meta); group.streaming = true; }
          if (type === 'agent_delta' && !group.complete) group.text += event.content || '';
          if (type === 'response') { group.text = event.content || group.text; group.complete = true; group.streaming = false; if (!item) this.serial++; }
        } else if (type === 'prompt' || (event.role === 'user' && !['human_edit'].includes(type))) {
          this.serial++;
          group = this.add(identity, 'user', event, meta); group.text = event.content || '';
        } else if (['tool_invocation','tool_output','runtime_output'].includes(type)) {
          const previous=this.operations.get(base);
          key = item ? base + ':operation:' + item : type!=='tool_invocation' && previous && !this.items.get(previous)?.complete ? previous : base + ':operation:unpaired-' + event.sequence;
          group = this.items.get(key) || this.add(key, 'operation', event, meta);
          if (!item) this.operations.set(base,key);
          if (type === 'tool_invocation') group.command = event.content || '';
          else if (type === 'runtime_output' && !group.complete) group.text += event.content || '';
          else if (type === 'tool_output') {
            group.text = event.content || group.text;
            group.complete = true;
            group.exit = meta.exit_code;
            group.duration = meta.duration_ms;
            try {
              const changes = JSON.parse(event.content);
              if (Array.isArray(changes) && changes.every(change => change && typeof change.path === 'string')) group.files = changes.map(change => change.path);
            } catch {}
          }
        } else if (['handoff','checkpoint','pause_boundary','stop','error','provider_error','recovery','queue','native_environment','human_edit'].includes(type)) {
          group = this.add(identity, ['error','provider_error'].includes(type) ? 'error' : 'milestone', event, meta);
          group.text = event.content || '';
          group.complete = true;
        }
        if (group) {
          group.updated = event.sequence; group.meta = {...group.meta, ...meta};
          if (meta.artifact_id && !group.artifacts.some(item=>item.id===meta.artifact_id)) group.artifacts.push({id:meta.artifact_id,title:type==='tool_invocation' ? 'Full command' : type==='response' ? 'Full response' : 'Full output'});
        }
      }
      return this.groups;
    }
    add(key, kind, event, meta) {
      const group = {key, kind, event, meta, text:'', command:'', complete:false, files:[], artifacts:[], exit:null, duration:null, updated:event.sequence};
      this.groups.push(group); this.items.set(key, group); return group;
    }
  }
  function operationTitle(group) {
    if (group.files.length) return 'Updated ' + group.files.length + ' file' + (group.files.length === 1 ? '' : 's');
    if (!group.command) return group.meta.channel === 'human_console' ? 'Console output' : 'Tool output';
    if (/\b(pytest|unittest|manage\.py test|npm (?:run )?test)\b/.test(group.command)) return 'Running tests';
    if (/\b(npm (?:run )?build|vite build)\b/.test(group.command)) return 'Building project';
    if (/^(?:\/[^ ]+\/)?(?:cat|sed|head|rg|ls)\b/.test(group.command)) return 'Reading project files';
    return group.meta.console_id ? 'Your command' : 'Running command';
  }
  class View {
    constructor(container, names) { this.container = container; this.names = names; this.nodes = new Map(); this.model = new Projection(); this.limit = 180; this.conversation = null; this.consumed=0;this.lastSeen=null; }
    reset(conversation) { this.conversation = conversation; this.model = new Projection(); this.nodes.clear(); this.container.replaceChildren();this.consumed=0;this.lastSeen=null; }
    render(events, conversation, state) {
      if (conversation !== this.conversation) this.reset(conversation);
      const nearBottom = this.container.scrollHeight - this.container.clientHeight - this.container.scrollTop < 100;
      if (this.consumed && events[this.consumed-1]?.event_id!==this.lastSeen) {this.model=new Projection();this.consumed=0;}
      const groups = this.model.append(events.slice(this.consumed));
      this.consumed=events.length;this.lastSeen=events.at(-1)?.event_id;
      if (!groups.length) { this.container.innerHTML = '<div class="agent-empty"><span class="agent-empty-mark">⇄</span><h2>Build something together.</h2><p>Select a worker and describe your task.<br>Your files, progress and context stay here.</p></div>'; return; }
      this.container.querySelector('.agent-empty')?.remove();
      const visible = groups.slice(-this.limit);
      const visibleKeys = new Set(visible.map(group => group.key));
      for (const [key, node] of this.nodes) if (!visibleKeys.has(key)) { node.remove(); this.nodes.delete(key); }
      if (groups.length > this.limit && !this.container.querySelector('.earlier-messages')) {
        const earlier = document.createElement('button'); earlier.className = 'earlier-messages'; earlier.textContent = 'Show earlier activity';
        earlier.onclick = () => { this.limit += 180; earlier.remove(); this.render(events, conversation, state); };
        this.container.prepend(earlier);
      }
      let previous = this.container.querySelector('.earlier-messages');
      for (const group of visible) {
        let node = this.nodes.get(group.key);
        if (!node) { node = document.createElement('article'); node.className = 'activity-item ' + group.kind; node.dataset.activityKey = group.key; this.nodes.set(group.key, node); }
        const signature = [group.updated, state, this.names(group.event.worker_id)].join(':');
        if (node.dataset.version !== signature) {
          const opened = node.querySelector('details')?.open;
          node.innerHTML = this.html(group, state);
          if (opened && node.querySelector('details')) node.querySelector('details').open = true;
          node.dataset.version = signature;
        }
        if (previous ? previous.nextSibling !== node : this.container.firstChild !== node) this.container.insertBefore(node, previous ? previous.nextSibling : this.container.firstChild);
        previous = node;
      }
      if (nearBottom) this.container.scrollTop = this.container.scrollHeight;
      const jump = document.getElementById('jumpLatest');
      if (jump) jump.hidden = nearBottom;
    }
    html(group, state) {
      const name = group.meta.console_id ? 'Your console' : group.meta.channel==='human_git' ? 'Your Git command' : group.event.worker_id ? this.names(group.event.worker_id) : group.kind === 'user' ? 'You' : 'Relay';
      const time = new Date(group.event.timestamp).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      const records=group.artifacts.map(item=>'<button class="record-link" data-artifact="' + escape(item.id) + '">' + escape(item.title) + ' ↗</button>').join('');
      if (group.kind === 'user' || group.kind === 'assistant') {
        const streaming = group.kind === 'assistant' && !group.complete && ['running','pausing'].includes(state);
        const origin = group.meta.execution_mode === 'native' ? 'NATIVE LOCAL' : group.meta.origin === 'simulation' ? 'SIMULATED' : group.meta.execution_mode === 'organization_api' ? 'ORG API' : '';
        return '<div class="message-heading"><span class="message-avatar">' + (group.kind === 'user' ? 'Y' : '⇄') + '</span><strong>' + escape(name) + '</strong><span class="origin-label">' + escape(origin) + '</span><time>' + escape(time) + '</time></div><div class="message-body' + (streaming ? ' streaming' : '') + '">' + markdown(group.kind === 'assistant' ? response(group.text) : group.text) + records + '</div>';
      }
      if (group.kind === 'operation') {
        const failed = group.exit != null && group.exit !== 0;
        const running = !group.complete && ['running','pausing'].includes(state);
        const status = running ? 'Running' : group.exit != null ? 'Exit ' + group.exit : group.complete ? 'Recorded' : 'Stopped';
        return '<details class="operation-card ' + (failed ? 'failed' : '') + '"><summary><span class="operation-icon' + (running ? ' spinning' : '') + '">' + (running ? '◌' : failed ? '!' : group.files.length ? '±' : '›_') + '</span><div><strong>' + escape(operationTitle(group)) + '</strong><code>' + escape(group.command || group.files.join(', ') || 'Recorded tool result') + '</code></div><span class="operation-result">' + escape(group.duration ? (group.duration / 1000).toFixed(1) + 's · ' + status : status) + '</span><span class="chevron">⌄</span></summary><div class="operation-body"><div class="operation-byline">' + escape(name) + ' · ' + escape(group.meta.console_id ? 'HUMAN CONSOLE' : group.meta.channel==='human_git' ? 'PROJECT GIT' : group.meta.execution_mode === 'native' ? 'NATIVE LOCAL' : group.meta.origin === 'simulation' ? 'SIMULATED' : 'TOOL') + '</div>' + (group.command ? '<pre class="operation-command"><code>' + escape(group.command) + '</code></pre>' : '') + (group.files.length ? group.files.map(file => '<button class="changed-file" data-open-file="' + escape(file.replace(/^.*\/workspaces\/[abc]\/[a-f0-9]{32}\//,'').replace(/^\/workspace\//,'')) + '">± ' + escape(file.split('/').slice(-3).join('/')) + '</button>').join('') : '<pre>' + escape(group.text || 'Waiting for output…') + '</pre>') + records + '</div></details>';
      }
      if (group.kind === 'error') return '<div class="activity-error"><strong>Execution needs attention</strong><p>' + escape(group.text) + '</p></div>';
      const type = group.event.event_type;
      const checkpoint = type === 'checkpoint';
      const title = checkpoint ? 'Checkpoint saved' : type === 'handoff' ? 'Worker handoff' : type === 'pause_boundary' || type === 'stop' ? 'Execution paused or stopped' : type === 'native_environment' ? 'Project environment' : type === 'human_edit' ? 'File saved' : type === 'queue' ? 'Waiting for a worker' : 'Recovery';
      return '<div class="milestone-line"><span>' + (checkpoint ? '◇' : type === 'handoff' ? '⇄' : '·') + '</span><div><strong>' + title + '</strong><p>' + escape(checkpoint ? group.text.slice(0,12) + (group.meta.reason ? ' · ' + group.meta.reason : '') : group.text) + '</p></div><time>' + escape(time) + '</time></div>';
    }
  }
  root.RelayActivity = {Projection, View, response, markdown, metadata};
})(typeof window !== 'undefined' ? window : globalThis);
