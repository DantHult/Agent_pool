'use strict';
const assert=require('node:assert/strict');
require('../static/activity.js');
const {Projection,response,markdown}=globalThis.RelayActivity;
let sequence=0;
function event(type,content,item='message-1',worker='a',task='task-1') {
  return {event_id:'event-'+ ++sequence,sequence,conversation_id:'one',task_id:task,worker_id:worker,event_type:type,content,timestamp:'2026-10-24T12:00:00Z',metadata:JSON.stringify({provider_item_id:item,execution_mode:'native',origin:'codex'})};
}
const model=new Projection();
const tokens=Array.from('A coherent response.'.repeat(100),token=>event('agent_delta',token));
model.append(tokens);assert.equal(model.groups.length,1);assert.equal(model.groups[0].text,'A coherent response.'.repeat(100));
model.append(tokens);assert.equal(model.groups.length,1);assert.equal(model.groups[0].text,'A coherent response.'.repeat(100));
model.append([event('response','{"summary":"Finished","test_results":["Two tests passed"]}')]);
assert.equal(model.groups.length,1);assert.match(response(model.groups[0].text),/Finished/);assert.match(response(model.groups[0].text),/Two tests passed/);
const summary=event('response','Finished',null);summary.metadata=JSON.stringify({origin:'relay'});model.append([summary]);assert.equal(model.groups.length,1);
model.append([event('tool_invocation','python3 -m unittest','command-1'),event('runtime_output','hello','command-1'),event('runtime_output',' world','command-1'),event('tool_output','hello world','command-1')]);
assert.equal(model.groups.length,2);assert.equal(model.groups[1].text,'hello world');assert.equal(model.groups[1].complete,true);
model.append([event('agent_delta','Other worker','message-1','b')]);assert.equal(model.groups.length,3);
model.append([event('session_started','New thread','session','a'),event('agent_delta','Another turn','message-1','a')]);assert.equal(model.groups.length,4);
model.append([event('reasoning_delta','Never visible')]);assert.equal(model.groups.length,4);
model.append([event('handoff','A → B','handoff')]);assert.equal(model.groups[4].kind,'milestone');
assert(!markdown('<img src=x onerror=alert(1)>').includes('<img'));
assert(markdown('```js\nconst value = "<safe>";\n```').includes('&lt;safe&gt;'));
assert.equal(response('{"summary":"Streaming partial'), 'Streaming partial');
const legacy=new Projection();
legacy.append([event('tool_invocation','python3 -m unittest',null),event('runtime_output','chunk',null),event('tool_output','chunk',null)]);
assert.equal(legacy.groups.length,1);assert.equal(legacy.groups[0].text,'chunk');
console.log('PASS: token coalescing, replay deduplication, final replacement, tool pairing, run/worker boundaries, handoff, and safe formatting');
