import base64
from flask import request,render_template,jsonify
from app.errors import DomainError
from app.config import TITLE,MODULES,SHORT
from app.workflow import run,load
from app import store

def payload():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','CSV内容必须是文本')
    sample=data.get('sample',request.args.get('sample','dirty.csv'))
    content=data.get('csv')
    return (content.encode() if isinstance(content,str) else load(sample)),data.get('parameters',{})

def api(stage):
    content,parameters=payload();return jsonify(run(content,parameters,stage))

def page(stage,template):
    latest=store.history();state=store.get(latest[0]['id']) if latest else run(load('dirty.csv'),{})
    source=load(request.args['sample']) if 'sample' in request.args else base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
    data=run(source,state.get('manifest',{}).get('parameters',{}),stage)
    # 页面统一补齐封账与延后账字段，低阶段预览也能看清本期采用与挡下的补卡。
    for key,default in (('deferred',[]),('carried',[]),('carry_in',[]),('pin_keys',[]),('carried_keys',[]),('adjustments',[])):
        data.setdefault(key,default)
    data.setdefault('closed',False);data.setdefault('period',None);data.setdefault('next_period',None);data.setdefault('close',None)
    data.setdefault('historical_lines',0)
    periods={'closes':store.list_closes(),'deferrals':store.deferral_ledger()}
    return render_template(template,title=TITLE,short=SHORT,modules=MODULES,current=stage,data=data,run=state,history=latest,periods=periods)
