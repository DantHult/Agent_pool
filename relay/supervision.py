"""Conservative native turn decisions. No account rotation or total turn budget."""
import hashlib
import json


def decision(progress):
    state=progress.get('completion_state')
    if state in ('blocked','needs_authorization','uncertain'):return state
    # Failing tests with concrete next steps are routine diagnosis, not a
    # blocker; repeated turns without new evidence are bounded by the caller.
    if progress.get('pending_steps'):return 'continue'
    if progress.get('errors'):return 'blocked'
    if state=='continue':return 'uncertain'  # No actionable remaining step.
    if state=='completed' and progress.get('acceptance_evidence') and progress.get('completed_work'):
        return 'completed'
    # Older CLI fixtures/clients have no explicit completion fields. Require a
    # structured final result with completed work and recorded test evidence.
    if state is None and progress.get('summary') and progress.get('completed_work') and progress.get('test_results'):
        return 'completed'
    return 'uncertain'


def evidence_signature(progress, source_signature):
    # Summaries and pending-step wording alone do not establish progress.
    value=[source_signature,sorted(progress.get('completed_work',[])),sorted(progress.get('test_results',[]))]
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
