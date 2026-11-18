import sqlite3,json,os,hashlib
from datetime import datetime,timezone
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS closes (period TEXT PRIMARY KEY, run_id TEXT NOT NULL, review_version INTEGER NOT NULL, closed_at TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, bundle TEXT NOT NULL, pin TEXT NOT NULL, deferred TEXT NOT NULL, signature TEXT NOT NULL, carried TEXT NOT NULL DEFAULT "[]")')
    try:db.execute('ALTER TABLE closes ADD COLUMN carried TEXT NOT NULL DEFAULT "[]"')
    except sqlite3.OperationalError:pass
    db.execute('CREATE TABLE IF NOT EXISTS deferrals (key TEXT PRIMARY KEY, source_period TEXT NOT NULL, target_period TEXT NOT NULL, employee TEXT NOT NULL, shift_day TEXT NOT NULL, punch TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, adopted_by TEXT, adopted_at TEXT)')
    return db

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    # 被挡下的晚到补卡登记到延后账，保证未进入本期的记录不丢失且可下期带入。
    record_deferrals(body.get('deferred',[]))
    return get(run_id)

def _render(r):
    body=json.loads(r['body'])
    # 旧批次不一定带封账字段，补默认值而不改写已存证据。
    for key,default in (('deferred',[]),('carried',[]),('carry_in',[]),('pin_keys',[]),('carried_keys',[]),('adjustments',[])):body.setdefault(key,default)
    body.setdefault('closed',False);body.setdefault('period',None);body.setdefault('next_period',None);body.setdefault('close',None)
    body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'])
    return body

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    return _render(r)

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        row=db.execute('SELECT body FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','批次不存在',404)
        body=json.loads(row['body'])
        period=body.get('period')
        if period and db.execute('SELECT 1 FROM closes WHERE period=?',(period,)).fetchone():
            raise DomainError('PERIOD_CLOSED','周期已封账，补卡请走下一期',409)
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def _close_row(r):return dict(r) if r else None

def find_close(period):
    if not period:return None
    with connection() as db:
        r=db.execute('SELECT period,run_id,review_version,closed_at,evidence_sha256,deferred,pin,carried FROM closes WHERE period=?',(period,)).fetchone()
    if not r:return None
    out=_close_row(r)
    out['deferred']=json.loads(out['deferred']);out['pin']=json.loads(out['pin']);out['carried']=json.loads(out['carried'])
    return out

def first_open_after(period):
    """延后账的落期：从下一期起找到第一个尚未封账的周期，避免补卡卡在已封账期。"""
    from app.periods import next_period
    target=next_period(period)
    while find_close(target):target=next_period(target)
    return target

def close_period(period, run_id, version):
    """把已复核通过的周期冻结为当前采用版本。

    只接受复核通过的批次；同一采用依据重复封账视为幂等，采用依据不同则拒绝，
    失败时事务不写入，旧版证据保持不变。冻结的是复核时看到的批次快照，
    不重新计算，保证证据哈希稳定可核对。
    """
    from app.periods import build_pin
    from app.modules.m7_archive.export_out import bundle, checksum
    with connection() as db:
        r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    if r['decision']!='通过':raise DomainError('REVIEW_REQUIRED','工资尚未复核通过，不能封账',409)
    if r['version']!=version:raise DomainError('STALE','复核版本已更新，请刷新',409)
    state=json.loads(r['body'])
    if state.get('period')!=period:raise DomainError('PERIOD','批次不属于该周期')
    if state.get('deferred'):
        raise DomainError('DEFERRED_PRESENT','尚有未决补卡，不能封账',409)
    adopting=set(state.get('carried_keys',[]))
    if any(d['key'] not in adopting for d in pending_deferrals(period)):
        # 延后账里有本期应带入、但本批未采用的补卡，先采用再封账。
        raise DomainError('DEFERRED_PRESENT','延后账仍有待采用补卡，不能封账',409)
    if not state.get('reconcile',{}).get('balanced'):
        raise DomainError('UNBALANCED','对账未平，不能封账',409)
    if not state.get('shifts'):
        raise DomainError('EMPTY_PERIOD','本期无可采用班次，不能封账',409)
    pin=build_pin(state);deferred=[]
    carried={'rows':[r for r in state.get('rows',[]) if r.get('_carry_in')],'keys':state.get('carried_keys',[])}
    evidence=checksum(state);text=bundle(state)
    signature=hashlib.sha256(json.dumps({'run_id':run_id,'pin':pin,'sha':evidence},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with connection() as db:
        existing=db.execute('SELECT * FROM closes WHERE period=?',(period,)).fetchone()
        if existing:
            if existing['signature']==signature:return find_close(period)
            raise DomainError('PERIOD_CLOSED','该周期已按另一版采用依据封账',409)
        db.execute('INSERT INTO closes(period,run_id,review_version,closed_at,evidence_sha256,bundle,pin,deferred,signature,carried) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (period,run_id,version,datetime.now(timezone.utc).isoformat(),evidence,text,
                    json.dumps(pin,ensure_ascii=False,sort_keys=True),json.dumps(deferred,ensure_ascii=False,sort_keys=True),signature,
                    json.dumps(carried,ensure_ascii=False,sort_keys=True)))
        # 本期实际带入的延后补卡在封账时核销，避免下一期重复计入。
        for key in state.get('carried_keys',[]):
            db.execute('UPDATE deferrals SET adopted_by=?,adopted_at=? WHERE key=? AND adopted_by IS NULL',
                       (run_id,datetime.now(timezone.utc).isoformat(),key))
        # 本期未采用的待办补卡顺延到下一个未封账周期，不能卡在已封账期。
        target=first_open_after(period)
        for row in db.execute('SELECT key FROM deferrals WHERE target_period=? AND adopted_by IS NULL',(period,)).fetchall():
            db.execute('UPDATE deferrals SET target_period=? WHERE key=?',(target,row['key']))
    return find_close(period)

def close_bundle(period):
    with connection() as db:
        r=db.execute('SELECT bundle FROM closes WHERE period=?',(period,)).fetchone()
    return r['bundle'] if r else None

def list_closes():
    with connection() as db:
        rows=db.execute('SELECT period,run_id,review_version,closed_at,evidence_sha256,deferred FROM closes ORDER BY period DESC').fetchall()
    out=[]
    for r in rows:
        item=dict(r);item['deferred']=len(json.loads(item['deferred']));out.append(item)
    return out

def _version(entry):
    try:return float(entry.get('version') or 0)
    except (TypeError,ValueError):return 0.0

def record_deferrals(entries):
    """晚到补卡入延后账。以稳定键幂等，重跑不会新增重复记录。

    落期跳过已封账周期，防止补卡被记到已复核的期次而永久搁置。
    同一卡位（员工/归属日/方向）的更新版本会顶替尚未采用的旧补卡。
    """
    if not entries:return
    now=datetime.now(timezone.utc).isoformat()
    with connection() as db:
        for entry in entries:
            if db.execute('SELECT 1 FROM deferrals WHERE key=?',(entry['key'],)).fetchone():
                continue
            slot=db.execute('SELECT key,body FROM deferrals WHERE employee=? AND shift_day=? AND punch=? AND adopted_by IS NULL',
                            (entry.get('employee',''),entry.get('day',''),entry.get('punch',''))).fetchall()
            if any(_version(json.loads(r['body'])) > _version(entry) for r in slot):
                # 同卡位已有更新版本待采用，旧版不覆盖，避免重提旧补卡回退。
                continue
            for r in slot:
                db.execute('DELETE FROM deferrals WHERE key=?',(r['key'],))
            target=first_open_after(entry['period'])
            stored=dict(entry,target_period=target)
            db.execute('INSERT OR REPLACE INTO deferrals(key,source_period,target_period,employee,shift_day,punch,body,created_at,adopted_by,adopted_at) VALUES (?,?,?,?,?,?,?,?,NULL,NULL)',
                (entry['key'],entry['period'],target,entry.get('employee',''),entry.get('day',''),entry.get('punch',''),
                 json.dumps(stored,ensure_ascii=False,sort_keys=True),now))

def pending_deferrals(period):
    """目标期可带入、且尚未被采用的延后补卡。"""
    with connection() as db:
        rows=db.execute('SELECT body FROM deferrals WHERE target_period=? AND adopted_by IS NULL ORDER BY key',(period,)).fetchall()
    return [json.loads(r['body']) for r in rows]

def deferrals_for(period):
    """本目标期的延后账全量（含已采用），用于展示与核对。"""
    with connection() as db:
        rows=db.execute('SELECT body FROM deferrals WHERE target_period=? ORDER BY key',(period,)).fetchall()
    return [json.loads(r['body']) for r in rows]

def deferral_ledger():
    with connection() as db:
        rows=db.execute('SELECT source_period,target_period,employee,shift_day,punch,adopted_by,body FROM deferrals ORDER BY target_period DESC,key').fetchall()
    return [dict(r, **json.loads(r['body'])) for r in rows]
