"""Test-only App Server protocol peer. No credentials or provider requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

home=Path(os.environ['CODEX_HOME']);profile=home.name;guard=threading.Lock();turn_cancel=threading.Event();cwd=None;thread=None;turn=None;started=False;turn_count=0;quota_hit=False
scenario_file=home/'scenario.json'
scenario=json.loads(scenario_file.read_text()) if scenario_file.exists() else {}

def send(message):
    with guard:print(json.dumps(message),flush=True)
def reply(number,value):send({'id':number,'result':value})
def notify(method,params):send({'method':method,'params':params})
def execute(params):
    global quota_hit
    working=Path(params['cwd']);text=params['input'][0]['text']
    continuous=scenario.get('continuous',0)
    complete=scenario.get('complete') or bool(continuous and turn_count>=continuous)
    with (home/'turns.jsonl').open('a') as output:output.write(json.dumps({'thread':thread,'turn':turn,'text':text,'cwd':str(working)})+'\n')
    (home/'received.json').write_text(json.dumps({'cwd':str(working),'text':text,'permissions':params.get('permissions'),'keys_present':[k for k in ('OPENAI_API_KEY','CODEX_API_KEY','RELAY_ORG_OPENAI_API_KEY') if k in os.environ]}))
    notify('item/started',{'threadId':thread,'item':{'id':'inspect','type':'commandExecution','command':'read existing project and modify source'}})
    if scenario.get('approval'):
        send({'id':900,'method':'item/commandExecution/requestApproval','params':{'threadId':thread,'command':'read outside workspace'}})
        send({'id':901,'method':'account/chatgptAuthTokens/refresh','params':{}})
    if scenario.get('quota') or scenario.get('quota_after') and turn_count>scenario['quota_after'] or scenario.get('quota_once') and not quota_hit:
        quota_hit=True
        notify('error',{'threadId':thread,'error':{'message':'Native usage limit reached','codexErrorInfo':'UsageLimitExceeded'}})
        notify('turn/completed',{'threadId':thread,'turn':{'id':turn,'status':'failed'}});return
    (working/'native_tools.py').write_text("def normalize(text):\n    return ' '.join(text.lower().split())\n" if profile!='a' or complete else "def normalize(text):\n    return text.strip()\n")
    notify('item/completed',{'threadId':thread,'item':{'id':'inspect','type':'commandExecution','aggregatedOutput':'mock transport wrote genuine temporary source file','exitCode':0}})
    if scenario.get('crash'):os._exit(7)
    notify('item/reasoning/textDelta',{'threadId':thread,'delta':'private-reasoning-must-not-persist'})
    if scenario.get('child'):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        (home/'child.pid').write_text(str(child.pid))
    delay=scenario.get('delay',0);deadline=time.monotonic()+delay
    while time.monotonic()<deadline:
        if turn_cancel.wait(.02):notify('turn/completed',{'threadId':thread,'turn':{'id':turn,'status':'interrupted'}});return
    tests=[]
    if profile=='c' or complete:
        (working/'test_native_tools.py').write_text("import unittest\nfrom native_tools import normalize\nclass NativeTests(unittest.TestCase):\n def test_spaces(self): self.assertEqual(normalize(' A   B '),'a b')\n def test_empty(self): self.assertEqual(normalize(''),'')\n")
        notify('item/started',{'threadId':thread,'item':{'id':'tests','type':'commandExecution','command':'python3 -m unittest test_native_tools'}})
        cmd=[sys.executable,'-I','-c',"import sys,unittest;sys.path.insert(0,'.');r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('.',pattern='test_native_tools.py'));sys.exit(not r.wasSuccessful())"]
        # Do not inherit the App Server stdin pipe. On Windows that handle stays
        # open in the parent reader and the child blocks before the tests run.
        result=subprocess.run(cmd,cwd=working,capture_output=True,text=True,stdin=subprocess.DEVNULL,timeout=60);tests=[result.stdout+result.stderr]
        notify('item/completed',{'threadId':thread,'item':{'id':'tests','type':'commandExecution','aggregatedOutput':tests[0],'exitCode':result.returncode}})
    progress={'summary':'TEST-ONLY native RPC fixture phase '+profile,'completed_work':['Updated native_tools.py'],
              'pending_steps':['Finish remaining code and tests'] if profile!='c' and not complete else [],'test_results':tests,'errors':[],'limitations':['Mock protocol peer; no real Codex inference'] ,'decisions':['Use normalized whitespace']}
    if continuous:
        progress.update({'completion_state':'completed' if complete else 'continue','acceptance_evidence':['Two tests passed'] if complete else []})
    if scenario.get('uncertain'):progress.update({'completion_state':'uncertain','pending_steps':[]})
    if scenario.get('needs_authorization'):progress.update({'completion_state':'needs_authorization','pending_steps':['Access a forbidden resource']})
    notify('item/completed',{'threadId':thread,'item':{'id':'message','type':'agentMessage','text':json.dumps(progress)}})
    notify('turn/completed',{'threadId':thread,'turn':{'id':turn,'status':'completed'}})

for line in sys.stdin:
    message=json.loads(line);method=message.get('method');number=message.get('id');params=message.get('params',{})
    if method is None:
        with (home/'approval-replies.jsonl').open('a') as output:output.write(json.dumps(message)+'\n')
    elif method=='initialize':started=True;reply(number,{'userAgent':'mock-native-app-server'})
    elif method=='initialized':pass
    elif method=='account/read':reply(number,{'account':{'type':scenario.get('auth','chatgpt'),'email':profile+'@fixture.invalid','planType':'pro'},'requiresOpenaiAuth':True})
    elif method=='account/rateLimits/read':
        # Read live, so a test can model the provider's window resetting.
        limits=(json.loads(scenario_file.read_text()) if scenario_file.exists() else {}).get('limits')
        if limits is None:send({'id':number,'error':{'code':-32601,'message':'Unsupported test method'}})
        else:
            stamp=int(time.time())
            reply(number,{'rateLimits':{'primary':{'usedPercent':limits.get('five_hour',100 if quota_hit else 10),'windowDurationMins':300,'resetsAt':stamp+3600},
                                        'secondary':{'usedPercent':limits.get('weekly',20),'windowDurationMins':10080,'resetsAt':stamp+3*86400}},
                          'ordinaryUsageAllowed':limits.get('allowed',not quota_hit)})
    elif method=='permissionProfile/list':reply(number,{'data':[{'id':'relay-native','allowed':True}]})
    elif method=='command/exec':reply(number,{'exitCode':0,'stdout':'native-python-ok' if 'native-python-ok' in params['command'][-1] else 'native-isolation-ok','stderr':''})
    elif method=='thread/start':
        assert started and params['permissions']=='relay-native' and 'sandbox' not in params
        cwd=params['cwd'];thread='mock-'+profile+'-'+uuid.uuid4().hex;reply(number,{'thread':{'id':thread}})
    elif method=='turn/start':
        assert params['cwd']==cwd and params['runtimeWorkspaceRoots']==[cwd]
        turn_count+=1;turn='turn-'+uuid.uuid4().hex;turn_cancel.clear();reply(number,{'turn':{'id':turn,'status':'inProgress'}})
        threading.Thread(target=execute,args=(params,),daemon=True).start()
    elif method=='turn/interrupt':turn_cancel.set();reply(number,{})
    elif number is not None:send({'id':number,'error':{'code':-32601,'message':'Unsupported test method'}})
