"""周期封账与晚到补卡的采用裁决。

裁决：已复核并封账的周期保持原采用版本，晚到补卡不回溯改写已复核工资；
补卡登记到延后账，留到下一期采用。同一班次只采用相互一致、且与复核版本
逐字段相同的进出卡；任一方向改版即整班留待下一期，不拼接新旧卡。
"""
import hashlib
import re
from collections import defaultdict

from app.errors import DomainError

# 参与"同版一致性"比较的字段，与 M3 跨夜配对保持一致。
SIGNATURE = ('version', 'at', 'hourly_rate', 'shift_start', 'shift_end', 'break_start', 'break_end', 'paid')
CARRY_FIELDS = ('id', 'employee', 'shift_day', 'punch', 'note')
PERIOD_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


def key_of(employee, day):
    return f'{employee}/{day}'


def signature(row):
    return tuple(str(row.get(k)) for k in SIGNATURE)


def matches_frozen(row, frozen_card):
    """已复核采用卡必须业务ID与采用字段逐项一致，才算同一张卡。"""
    return (frozen_card is not None
            and str(row.get('id')) == str(frozen_card.get('id'))
            and signature(row) == signature(frozen_card))


def next_period(period):
    year, month = (int(p) for p in period.split('-'))
    return f'{year + 1}-01' if month == 12 else f'{year}-{month + 1:02d}'


def period_of(day):
    return str(day)[:7]


def resolve_period(state):
    """周期优先取参数；未指定时要求本批归属日同月，跨月必须显式指定。"""
    parameters = state.get('parameters') or {}
    requested = parameters.get('period')
    months = sorted({period_of(r['shift_day']) for r in state.get('rows', []) if r.get('shift_day')})
    if requested is not None:
        requested = str(requested)
        if not PERIOD_RE.match(requested):
            raise DomainError('PERIOD', '周期格式应为 YYYY-MM')
        return requested
    if not months:
        return None
    if len(months) > 1:
        raise DomainError('PERIOD', '跨月批次需指定 period')
    return months[0]


def card(row):
    """把一行打卡压缩成可在封账快照与延后账中复用的采用卡。"""
    if not row:
        return None
    out = {k: row.get(k) for k in CARRY_FIELDS if k in row}
    for field in SIGNATURE:
        out[field] = None if row.get(field) is None else str(row.get(field))
    out['_line'] = row.get('_line')
    out['_raw'] = row.get('_raw', {})
    return out


def build_pin(body):
    """从待封账批次中取每个班次实际采用的进出卡与金额，作为冻结依据。

    跨期调整班次（归属日不属于本期）不写入本期冻结快照。
    """
    from decimal import Decimal
    amounts = defaultdict(lambda: Decimal(0))
    for item in body.get('calculations', []):
        amounts[item.get('id')] += Decimal(str(item.get('amount', '0')))
    pin = {}
    for shift in body.get('shifts', []):
        if body.get('period') and period_of(shift['day']) != body['period']:
            continue
        pin[key_of(shift['employee'], shift['day'])] = {
            'employee': shift['employee'], 'day': shift['day'],
            'in': card(shift.get('in')), 'out': card(shift.get('out')),
            'amount': str(amounts.get(shift['id'], Decimal(0))),
        }
    return pin


def deferral_key(row):
    """与行号无关的稳定标识：同一张补卡重跑或多批提交都归并为一条记录。"""
    parts = [row.get('employee'), row.get('shift_day'), row.get('punch')] + [str(row.get(k)) for k in SIGNATURE]
    return hashlib.sha256('|'.join(map(str, parts)).encode('utf-8')).hexdigest()[:16]


def defer_entry(row, period, reason, target=None):
    return {
        'key': deferral_key(row), 'employee': row.get('employee'), 'day': row.get('shift_day'),
        'punch': row.get('punch'), 'version': str(row.get('version')), 'at': row.get('at'),
        'id': row.get('id'), 'line': row.get('_line'), 'code': 'DEFERRED', 'reason': reason,
        'period': period, 'target_period': target or next_period(period),
        'card': card(row), 'raw': row.get('_raw', {}),
    }


def _carried_row(frozen_card, kind, origin=None):
    row = dict(frozen_card)
    row['_carried'] = True
    row['_origin_line'] = frozen_card.get('_line')
    if origin:
        row['_origin_period'] = origin
    row['_line'] = f"carry:{row.get('employee')}/{row.get('shift_day')}/{kind.upper()}"
    return row


def guard(state):
    """在 M3 配对前应用封账快照与延后账。"""
    from app import store  # 延迟导入，避免 store/periods 相互依赖

    state.setdefault('deferred', [])
    state.setdefault('carried', [])
    state.setdefault('carry_in', [])
    state.setdefault('pin_keys', [])
    state['historical_lines'] = 0
    state['carried_keys'] = []
    state['closed'] = False
    state['close'] = None
    period = resolve_period(state)
    state['period'] = period
    state['next_period'] = next_period(period) if period else None
    if not period:
        return state

    close = store.find_close(period)
    if close:
        return _freeze(state, close, period)
    return _carry_in(state, period)


def _freeze(state, close, period):
    """已封账周期：按复核快照复算，晚到或改版补卡一律挡下并登记延后账。

    本期复核时带入的跨期补卡随封账快照一并复原，保证重跑结果与已复核工资一致。
    """
    from app import store

    pin = close.get('pin') or {}
    frozen_carry = close.get('carried') or {}
    target = store.first_open_after(period)
    state['closed'] = True
    state['pin_keys'] = sorted(pin)
    state['next_period'] = target
    state['close'] = {k: close[k] for k in ('period', 'run_id', 'review_version', 'evidence_sha256') if k in close}
    state['carried_keys'] = list(frozen_carry.get('keys') or [])

    grouped = defaultdict(list)
    foreign = []
    historical = 0
    for row in state['rows']:
        day_period = period_of(row.get('shift_day'))
        if day_period != period:
            if store.find_close(day_period):
                # 已复核周期的原始行，本期不重复计入。
                historical += 1
            else:
                foreign.append(row)
            continue
        grouped[key_of(row.get('employee'), row.get('shift_day'))].append(row)

    deferred = {}
    for row in foreign:
        # 归属其他未封账月份的补卡不丢记录，落到归属月之后第一个未封账周期。
        owner = period_of(row.get('shift_day'))
        target = store.first_open_after(owner)
        entry = defer_entry(row, owner, 'FOREIGN_PERIOD', target)
        deferred.setdefault(entry['key'], entry)
    kept_by_key = defaultdict(list)
    for key, rows in grouped.items():
        frozen = pin.get(key)
        if frozen is None:
            # 已封账期里新出现的班次不能回溯入账。
            for row in rows:
                entry = defer_entry(row, period, 'SHIFT_NOT_IN_REVIEWED', target)
                deferred.setdefault(entry['key'], entry)
            continue
        for row in rows:
            frozen_card = frozen.get('in' if row.get('punch') == 'IN' else 'out') \
                if row.get('punch') in ('IN', 'OUT') else None
            if matches_frozen(row, frozen_card):
                kept_by_key[key].append(row)
            else:
                # 改版卡本期不采用；已复核班次仍按冻结卡复算，整班一致。
                entry = defer_entry(row, period, 'PERIOD_CLOSED', target)
                deferred.setdefault(entry['key'], entry)

    carried = []
    for key, frozen in pin.items():
        matched = {row.get('punch') for row in kept_by_key.get(key, [])}
        for kind in ('in', 'out'):
            if frozen.get(kind) and kind.upper() not in matched:
                carried.append(_carried_row(frozen[kind], kind))

    frozen_rows = []
    for row in (frozen_carry.get('rows') or []):
        item = dict(row)
        # 复核时采用的补卡按快照复原，属沿用证据，不再计入本期来源输入。
        item['_carried'] = True
        item['_carry_in'] = True
        frozen_rows.append(item)
    state['rows'] = [row for rows in kept_by_key.values() for row in rows] + carried + frozen_rows
    state['deferred'] = sorted(deferred.values(), key=lambda d: d['key'])
    state['carried'] = sorted(carried, key=lambda r: r['_line'])
    state['carry_in'] = sorted(store.deferrals_for(period), key=lambda d: d['key'])
    state['historical_lines'] = historical
    return state


def _carry_in(state, period):
    """未封账周期：带入延后账中目标期为本期的补卡，并补齐同班次冻结卡。

    来源文件里属于已封账周期的原始行不再重新采用（已由冻结快照复算），
    只有延后账里明确指向本期的补卡才允许进入本期。
    """
    from app import store

    pending = {d['key']: d for d in store.pending_deferrals(period)}
    foreign = []
    matched = {}
    kept = []
    historical = 0
    revised = []
    for row in state['rows']:
        day_period = period_of(row.get('shift_day'))
        if day_period == period:
            kept.append(row)
            continue
        hit = pending.get(deferral_key(row))
        if hit is not None:
            matched[hit['key']] = hit
            row['_carry_in'] = True
            row['_origin_period'] = hit['period']
            row['_deferral_key'] = hit['key']
            kept.append(row)
            continue
        close = store.find_close(day_period)
        if close:
            frozen = (close.get('pin') or {}).get(key_of(row.get('employee'), row.get('shift_day'))) or {}
            card_row = frozen.get('in' if row.get('punch') == 'IN' else 'out')
            if matches_frozen(row, card_row):
                # 与已复核采用卡逐字段一致，上期已付，不重复计入。
                historical += 1
            else:
                # 已封账期的改版卡：本期不采用、也不静默丢弃，登记留到下一期。
                revised.append(row)
            continue
        foreign.append(row)
    if foreign:
        first = foreign[0]
        raise DomainError('PERIOD', f"补卡 {first.get('employee')} {first.get('shift_day')} 未在延后账内，不能计入 {period}")

    deferred = {}
    for row in revised:
        owner = period_of(row.get('shift_day'))
        entry = defer_entry(row, owner, 'PERIOD_CLOSED', store.first_open_after(owner))
        deferred.setdefault(entry['key'], entry)

    present = {(key_of(r.get('employee'), r.get('shift_day')), str(r.get('punch'))) for r in kept}
    injected = []
    for hit in pending.values():
        key = key_of(hit['employee'], hit['day'])
        slot = (key, str(hit['punch']))
        if slot not in present:
            # 本期文件未附该补卡时，从延后账补入，保证记录不丢失且下期可核对。
            row = _carried_row(hit['card'] or {}, str(hit['punch']).lower(), hit['period'])
            row['_carry_in'] = True
            injected.append(row)
            present.add(slot)
            matched.setdefault(hit['key'], hit)
        close = store.find_close(hit['period']) or {}
        frozen = (close.get('pin') or {}).get(key)
        if not frozen:
            continue
        for kind in ('in', 'out'):
            opposite = (key, kind.upper())
            if frozen.get(kind) and opposite not in present:
                # 另一方向沿用来源期复核冻结卡，保证同一班次进出卡一致。
                row = _carried_row(frozen[kind], kind, hit['period'])
                row['_carry_in'] = True
                injected.append(row)
                present.add(opposite)

    state['rows'] = kept + injected
    state['deferred'] = sorted(deferred.values(), key=lambda d: d['key'])
    state['carry_in'] = sorted(matched.values(), key=lambda d: d['key'])
    state['carried_keys'] = sorted(matched)
    state['historical_lines'] = historical
    return state


def adjustments(state):
    """为跨期带入的补卡算出相对已复核金额的补发或补扣，避免整班重付。"""
    from app import store
    from decimal import Decimal

    amounts = defaultdict(lambda: Decimal(0))
    for item in state.get('calculations', []):
        amounts[item.get('id')] += Decimal(str(item.get('amount', '0')))
    # 只有本期真正配对成班的补卡才结算差额；被隔离的补卡留待复核，不整班补扣。
    paired = {key_of(s['employee'], s['day']) for s in state.get('shifts', [])}
    out = []
    for key in state.get('carried_keys', []):
        period = state['period']
        hit = next((d for d in state.get('carry_in', []) if d['key'] == key), None)
        if not hit or key_of(hit['employee'], hit['day']) not in paired:
            continue
        close = store.find_close(hit['period']) or {}
        frozen = (close.get('pin') or {}).get(key_of(hit['employee'], hit['day']))
        if not frozen:
            continue
        before = Decimal(str(frozen.get('amount', '0')))
        after = amounts.get(key_of(hit['employee'], hit['day']), Decimal(0))
        delta = (after - before).quantize(Decimal('.01'))
        if delta == 0:
            continue
        out.append({'key': key, 'employee': hit['employee'], 'day': hit['day'],
                    'source_period': hit['period'], 'period': period,
                    'reviewed_amount': str(before.quantize(Decimal('.01'))),
                    'recomputed_amount': str(after.quantize(Decimal('.01'))),
                    'delta': str(delta), 'kind': '补发' if delta > 0 else '补扣',
                    'code': 'ADJUSTMENT'})
    return sorted(out, key=lambda a: a['key'])


def annotate(state):
    """在 M3 之后标记每个班次本期的采用来源与版本。"""
    pinned = set(state.get('pin_keys') or [])
    for shift in state.get('shifts', []):
        key = key_of(shift['employee'], shift['day'])
        shift['pinned'] = key in pinned
        shift['adopted_source'] = '已复核冻结' if key in pinned else '本期'
        shift['in_version'] = str(shift['in']['version']) if shift.get('in') else None
        shift['out_version'] = str(shift['out']['version']) if shift.get('out') else None
    return state
