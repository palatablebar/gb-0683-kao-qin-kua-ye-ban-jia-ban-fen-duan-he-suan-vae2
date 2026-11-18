from app.modules.m7_archive.export_out import checksum

def execute(state):
    deferred=state.get('deferred',[])
    state['archive']={'sha256':checksum(state),
        'evidence_count':len(state.get('calculations',[]))+len(state['quarantine'])+len(state.get('shifts',[]))+len(deferred)+len(state.get('adjustments',[])),
        'ready':state.get('reconcile',{}).get('balanced',False),
        'deferred_count':len(deferred),'adjustment_count':len(state.get('adjustments',[])),
        'next_period':state.get('next_period'),
        'period':state.get('period'),'closed':state.get('closed',False)}
    return state
