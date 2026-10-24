"""Bounded interpretation of official codex exec JSONL; never retain reasoning items."""
import re

PROGRESS_KEYS=('completed_work','pending_steps','decisions','test_results','errors','limitations')

def progress_from_message(text):
    import json
    try: value=json.loads(text)
    except (ValueError,TypeError): return {}
    if not isinstance(value,dict): return {}
    result={key:[str(item)[:2000] for item in value[key][:20]] for key in PROGRESS_KEYS if isinstance(value.get(key),list)}
    if isinstance(value.get('summary'),str): result['summary']=value['summary'][:12000]
    return result

def provider_error(event):
    """Classify explicit provider errors; a numeric quota is never inferred."""
    error=event.get('error') or event.get('message') or {}
    if isinstance(error,str): error={'message':error}
    if not isinstance(error,dict): error={}
    message=str(error.get('message') or event.get('message') or 'Provider execution failed')
    code=str(error.get('code') or event.get('code') or '')
    status=error.get('status_code',error.get('status',event.get('status_code')))
    if not isinstance(status,int):
        match=re.search(r'(?:HTTP(?:/\S+)?\s+|status(?:\s+code)?[\s:=]+)([45]\d\d)\b|\b(429)\s+Too Many Requests',message,re.I)
        status=int(next(group for group in match.groups() if group)) if match else None
    lower=(code+' '+message).lower()
    if code=='insufficient_quota' or 'insufficient_quota' in lower: category='insufficient_quota'
    elif code=='rate_limit_exceeded' or status==429 or 'rate_limit_exceeded' in lower: category='rate_limit_exceeded'
    elif status in (401,403) or code in ('invalid_api_key','authentication_error'): category='authentication_error'
    elif status and status>=500: category='provider_unavailable'
    elif any(word in lower for word in ('connection','network','stream disconnected','timed out','timeout')): category='transport_error'
    else: category=code or 'provider_error'
    retry=error.get('retry_after',event.get('retry_after'))
    if isinstance(retry,(int,float)) and not isinstance(retry,bool) and 0<=retry<=86400: retry=float(retry)
    else: retry=None
    return {'category':category,'provider_code':code or None,'http_status':status,'retry_after_seconds':retry,
            'retry_guidance':'Wait for shared organization capacity or resolve billing; switching workers does not reset this limit.' if category in ('rate_limit_exceeded','insufficient_quota') else 'Review the error and preserved work before an explicit retry. No coding turn is replayed automatically.',
            'message':message[:12000]}
