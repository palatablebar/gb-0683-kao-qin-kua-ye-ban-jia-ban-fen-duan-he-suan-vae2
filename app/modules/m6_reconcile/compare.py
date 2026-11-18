def balance(state):
    """行数对账：来源行必须全部落在采纳、隔离、延后、历史或空行/重复表头/完全重复之一。

    - 采纳：本期实际计入的来源行（含延后账带入的补卡）。
    - 历史：属于已封账周期、已由冻结快照复算的来源行，不重复计入。
    - 延后：被挡下、保留在延后账的晚到补卡。
    - 沿用：从冻结快照或延后账补入的卡，不属于本期来源输入，单列展示。
    """
    carried_rows=[r for r in state['rows'] if r.get('_carried')]
    adopted_carried=len(carried_rows)
    accepted=len([r for r in state['rows'] if not r.get('_carried')])
    quarantined_carried=len([q for q in state['quarantine'] if str(q.get('line','')).startswith('carry:')])
    isolated=len(state['quarantine'])-quarantined_carried
    deferred=len(state.get('deferred',[]))
    historical=state.get('historical_lines',0)
    accounted=accepted+isolated+deferred+historical+state['blank_count']+state['header_count']+state['duplicate_count']
    return {'input':state['input_count'],'accepted':accepted,'isolated':isolated,
        'deferred':deferred,'historical':historical,'adjustments':len(state.get('adjustments',[])),
        'carried':adopted_carried+quarantined_carried,'carried_quarantined':quarantined_carried,
        'blank':state['blank_count'],'headers':state['header_count'],'duplicates':state['duplicate_count'],
        'accounted':accounted,'balanced':accounted==state['input_count'],
        'business':state.get('business',{})}
