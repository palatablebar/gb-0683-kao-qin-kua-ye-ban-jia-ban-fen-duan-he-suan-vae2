import io,csv,json,hashlib

HEADER=['记录类型','批次','来源行','业务ID','原因码','内容']

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(HEADER)
    rid=state.get('run_id','preview')
    period={'period':state.get('period'),'closed':state.get('closed',False),'close':state.get('close'),'next_period':state.get('next_period')}
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['周期',rid,'','','',json.dumps(period,ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for shift in state.get('shifts',[]):
        writer.writerow(['班次',rid,'','','',json.dumps({k:shift.get(k) for k in ('id','employee','day','pinned','adopted_source','in_version','out_version')},ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('deferred',[]):writer.writerow(['延后',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state.get('adjustments',[]):writer.writerow(['调整',rid,'',row.get('employee',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
