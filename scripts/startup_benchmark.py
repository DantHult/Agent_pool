"""Repeatable Chrome startup profile; generated history, mocked native protocol.

No provider execution or real authentication is used. Both phases consume the
same persisted logical dataset and fresh Chrome caches on the same host.
"""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import platform
import statistics
import sys
import time
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_local_native_dashboard import LocalNativeDashboardTests  # noqa: E402
from relay.native import NativeCodexAdapter  # noqa: E402
from relay.store import Relay  # noqa: E402
from relay.workspace import Workspace  # noqa: E402


def run(label, count):
    from playwright.sync_api import sync_playwright
    output=ROOT/'ide-proof';output.mkdir(exist_ok=True)
    dataset_path=output/'startup-dataset.json'
    if not dataset_path.exists():
        records=[]
        for message in range(180):
            records.append(['prompt','Inspect source and implement change '+str(message),'user',{}])
            for fragment in range(18):records.append(['agent_delta','visible fragment '+str(fragment)+' ','assistant',{'origin':'codex','provider_item_id':'message-'+str(message)}])
            records.append(['response','Completed change '+str(message),'assistant',{'origin':'codex','provider_item_id':'message-'+str(message)}])
        dataset_path.write_text(json.dumps(records))
    data=dataset_path.read_bytes();records=json.loads(data)
    case=LocalNativeDashboardTests();case.setUp();samples=[];metrics={}
    patches=[]
    def timed(cls,name):
        original=getattr(cls,name)
        def wrapped(*args,**kwargs):
            start=time.perf_counter()
            try:return original(*args,**kwargs)
            finally:
                elapsed=(time.perf_counter()-start)*1000
                row=metrics.setdefault(cls.__name__+'.'+name,{'calls':0,'total_ms':0,'max_ms':0})
                row['calls']+=1;row['total_ms']+=elapsed;row['max_ms']=max(row['max_ms'],elapsed)
        item=patch.object(cls,name,wrapped);item.start();patches.append(item)
    try:
        for profile in ('a','b','c'):case.enroll(profile)
        r=case.server.relay
        with r.lock:
            ws=r.ws.path(case.project);(ws/'hello.py').write_text('print("startup-ready")\n');r.ws.checkpoint(case.project,'Benchmark source')
            r.db.executemany('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',[
                ('benchmark-'+str(i),'orgdemo',case.project,case.conversation,None,i+1,'2026-10-24T12:00:00+00:00',None,kind,role,content,json.dumps(meta))
                for i,(kind,content,role,meta) in enumerate(records)])
        for cls,name in ((Relay,'rows'),(Relay,'one'),(Workspace,'inspect'),(NativeCodexAdapter,'health')):timed(cls,name)
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
            for _ in range(count):
                context=browser.new_context(viewport={'width':1536,'height':960})
                # Relay fixture's own ephemeral cookie; never printed/persisted.
                context.add_cookies([{'name':case.server.cookie_name,'value':case.client.cookie.split('=',1)[1],'url':case.server.public_url}])
                context.add_init_script("sessionStorage.setItem('relay-editor-client','startup-benchmark-client-0123456')")
                context.add_init_script("window.__historyRender=[];let projected;Object.defineProperty(window,'RelayActivity',{configurable:true,get(){return projected;},set(value){projected=value;const render=value.View.prototype.render;value.View.prototype.render=function(...args){const start=performance.now();try{return render.apply(this,args);}finally{window.__historyRender.push(performance.now()-start);}};}});")
                page=context.new_page();requests=[];metrics.clear()
                page.on('requestfinished',lambda request:requests.append({'path':urlsplit(request.url).path,'duration_ms':request.timing.get('responseEnd',0),'bytes':request.sizes().get('responseBodySize',0)}) if '/api/' in request.url and '/stream' not in request.url else None)
                cdp=context.new_cdp_session(page);cdp.send('Performance.enable')
                page.goto(case.server.public_url)
                page.wait_for_selector('#fileTree [data-open-file="hello.py"]',timeout=120000)
                page.locator('#fileTree [data-open-file="hello.py"]').click()
                page.wait_for_function('() => window.RelayIDE?.activeFile==="hello.py" && window.RelayIDE.editor && !window.RelayIDE.editor.getOption(window.RelayMonaco.editor.EditorOption.readOnly)',timeout=120000)
                timing=page.evaluate('() => ({interactive_ms:performance.now(),navigation:performance.getEntriesByType("navigation")[0].toJSON(),nodes:document.querySelectorAll("*").length,activity_nodes:document.querySelectorAll("#timeline .activity-item").length,loaded_events:typeof events!=="undefined"?events.length:null,history_render_ms:window.__historyRender.reduce((sum,value)=>sum+value,0)})')
                cdp_metrics={m['name']:m['value'] for m in cdp.send('Performance.getMetrics')['metrics']}
                timing.update({'api':requests,'backend':json.loads(json.dumps(metrics)),'script_ms':cdp_metrics.get('ScriptDuration',0)*1000,'layout_ms':cdp_metrics.get('LayoutDuration',0)*1000,'js_heap_bytes':cdp_metrics.get('JSHeapUsedSize'),'python_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
                samples.append(timing);print('Sample '+str(len(samples))+': '+str(round(timing['interactive_ms'],1))+' ms',flush=True);context.close()
            browser.close()
        report={'label':label,'dataset_sha256':hashlib.sha256(data).hexdigest(),'event_count':len(records),'inference':'mocked protocol; actual HTTP/SQLite/Git/Chrome','cache':'fresh browser context per sample','environment':{'python':platform.python_version(),'platform':platform.platform(),'chrome':browser.version,'viewport':[1536,960]},'samples':samples,'median_interactive_ms':statistics.median(sample['interactive_ms'] for sample in samples)}
        (output/('startup-'+label+'.json')).write_text(json.dumps(report,indent=2))
        print(json.dumps({'label':label,'events':len(records),'median_interactive_ms':report['median_interactive_ms'],'samples_ms':[round(s['interactive_ms'],1) for s in samples],'loaded_events':[s['loaded_events'] for s in samples]},indent=2))
    finally:
        for item in reversed(patches):item.stop()
        case.tearDown()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--label',required=True);parser.add_argument('--samples',type=int,default=3);args=parser.parse_args()
    if not args.label.isalnum():parser.error('label must be alphanumeric')
    run(args.label,args.samples)
