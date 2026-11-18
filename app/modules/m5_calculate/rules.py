from datetime import date
from decimal import Decimal,ROUND_HALF_UP
from collections import defaultdict

def price(segments,holidays):
    normal_used=defaultdict(int);out=[]
    for s in sorted(segments,key=lambda r:(r['start'],r['employee'])):
        d=date.fromisoformat(s['calendar_day']);key=(s['employee'],s['calendar_day']);minutes=s['minutes'];rate=Decimal(str(s['rate']))
        if s['calendar_day'] in holidays:parts=[(minutes,Decimal(3),'HOLIDAY')]
        elif d.weekday()>=5:parts=[(minutes,Decimal(2),'REST')]
        else:
            regular=min(minutes,max(0,480-normal_used[key]));normal_used[key]+=minutes
            parts=[(regular,Decimal(1),'REGULAR'),(minutes-regular,Decimal('1.5'),'OVERTIME')]
        for count,multiplier,code in parts:
            if count:
                item=dict(s);item.update(minutes=count,multiplier=str(multiplier),code=code,amount=str((rate*count*multiplier/60).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)));out.append(item)
    return out

def apply(state):
    from app.periods import adjustments
    holidays=set(state['parameters'].get('holidays',['2026-10-01']))
    calculations=price(state['segments'],holidays)
    state['calculations']=calculations
    state['adjustments']=adjustments(state)
    # 跨期带入的补卡不重付整班：其已复核金额在上期已发，本期只计差额。
    current=[r for r in calculations if not r.get('carry_in')]
    total=sum((Decimal(r['amount']) for r in current),Decimal(0))
    delta=sum((Decimal(r['delta']) for r in state['adjustments']),Decimal(0))
    paid=sum((Decimal(str(r['paid'])) for r in state['rows']),Decimal(0))
    state['business']={'total':str(total),'adjustment':str(delta),'payable':str(total+delta),
        'paid':str(paid),'difference':str(total+delta-paid),
        'minutes':sum(r['minutes'] for r in current),'unit':'CNY',
        'carried_minutes':sum(r['minutes'] for r in calculations if r.get('carry_in'))};return state
