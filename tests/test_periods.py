import csv
import io

import pytest

from app import store
from app.errors import DomainError
from app.workflow import load, run


def csv_bytes(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


@pytest.fixture
def clean_rows():
    text = load('clean.csv').decode('utf-8-sig')
    return list(csv.DictReader(io.StringIO(text)))


def close_clean(period='2026-10'):
    state = run(load('clean.csv'), {'period': period})
    store.save(state['run_id'], state)
    store.review(state['run_id'], 1, '通过')
    store.close_period(period, state['run_id'], 2)
    return state


def test_reviewed_period_keeps_frozen_pay(clean_rows):
    base = close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1])
    late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    result = run(csv_bytes(rows + [late]), {'period': '2026-10'})
    # 已复核工资不因晚到补卡改变，且补卡整张进入下期。
    assert result['closed'] is True
    assert result['business']['total'] == base['business']['total']
    assert [d['employee'] for d in result['deferred']] == ['E001']
    assert result['deferred'][0]['target_period'] == '2026-11'
    e001 = next(s for s in result['shifts'] if s['employee'] == 'E001')
    assert e001['out']['at'] == '2026-10-01T22:00:00+00:00'
    assert e001['out_version'] == '1'
    assert e001['adopted_source'] == '已复核冻结'


def test_deferred_card_is_not_lost_on_rerun(clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    data = csv_bytes(rows + [late])
    first = store.save(*_pair(run(data, {'period': '2026-10'})))
    second = store.save(*_pair(run(data, {'period': '2026-10'})))
    assert first['deferred'] == second['deferred']
    # 重跑不新增重复延后记录。
    ledger = store.deferral_ledger()
    assert len(ledger) == 1 and ledger[0]['employee'] == 'E001'


def _pair(state):
    return state['run_id'], state


def test_single_card_revision_defers_whole_shift(clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    # 只改出卡时刻，进卡原样；本期仍整班采用已复核版本，不拼接新旧卡。
    revised = dict(rows[1]); revised.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    result = run(csv_bytes(rows[:1] + [revised] + rows[2:]), {'period': '2026-10'})
    e001 = next(s for s in result['shifts'] if s['employee'] == 'E001')
    assert {e001['in_version'], e001['out_version']} == {'1'}
    assert result['deferred'][0]['punch'] == 'OUT'


def test_deferral_carries_into_next_period(clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    data = csv_bytes(rows + [late])
    store.save(*_pair(run(data, {'period': '2026-10'})))
    nxt = run(data, {'period': '2026-11'})
    assert nxt['carry_in'] and nxt['carry_in'][0]['key'] in nxt['carried_keys']
    e001 = next(s for s in nxt['shifts'] if s['employee'] == 'E001')
    assert e001['out_version'] == '2'
    assert e001['in_version'] == '1'
    assert e001['in']['at'] == '2026-10-01T14:00:00+00:00'


def next_month_rows(clean_rows):
    out = []
    for index, employee in enumerate(('E001', 'E002')):
        a = dict(clean_rows[0]); a.update(id=f'N{index}a', employee=employee, shift_day='2026-11-01',
                                          at='2026-11-01T22:00:00+08:00')
        b = dict(clean_rows[1]); b.update(id=f'N{index}b', employee=employee, shift_day='2026-11-01',
                                          at='2026-11-02T06:00:00+08:00')
        out += [a, b]
    return out


def test_carry_in_is_adjustment_not_full_repay(clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    data = csv_bytes(rows + [late])
    store.save(*_pair(run(data, {'period': '2026-10'})))
    nxt = run(csv_bytes(next_month_rows(clean_rows) + [late]), {'period': '2026-11'})
    # 上期已付 220，补卡重算 240，本期只计 20 补发，不整班重付 240。
    assert [a['delta'] for a in nxt['adjustments']] == ['20.00']
    assert nxt['adjustments'][0]['kind'] == '补发'
    assert nxt['business']['total'] == '360.00'
    assert nxt['business']['adjustment'] == '20.00'
    assert nxt['business']['payable'] == '380.00'


def test_closed_period_rerun_reproduces_adjustments(clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    store.save(*_pair(run(csv_bytes(rows + [late]), {'period': '2026-10'})))
    data = csv_bytes(next_month_rows(clean_rows) + [late])
    nxt = store.save(*_pair(run(data, {'period': '2026-11'})))
    store.review(nxt['run_id'], 1, '通过')
    store.close_period('2026-11', nxt['run_id'], 2)
    again = run(data, {'period': '2026-11'})
    assert again['business'] == nxt['business']
    assert again['adjustments'] == nxt['adjustments']
    assert again['reconcile']['balanced'] is True
    # 已采用的跨期补卡不会被二次挡下。
    assert again['deferred'] == []


def test_reconcile_balances_with_deferrals(clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    data = csv_bytes(rows + [late])
    closed = run(data, {'period': '2026-10'})
    store.save(closed['run_id'], closed)
    assert closed['reconcile']['balanced'] is True
    assert closed['reconcile']['deferred'] == 1
    nxt = run(data, {'period': '2026-11'})
    assert nxt['reconcile']['balanced'] is True
    assert nxt['reconcile']['historical'] == len(rows)


def test_review_after_close_is_rejected(clean_rows):
    base = close_clean()
    with pytest.raises(DomainError) as error:
        store.review(base['run_id'], 2, '退回')
    assert error.value.code == 'PERIOD_CLOSED' and error.value.status == 409


def test_close_requires_review(clean_rows):
    state = run(load('clean.csv'), {'period': '2026-10'})
    store.save(state['run_id'], state)
    with pytest.raises(DomainError) as error:
        store.close_period('2026-10', state['run_id'], 1)
    assert error.value.code == 'REVIEW_REQUIRED'


def test_close_is_idempotent_and_keeps_old_evidence(clean_rows):
    base = close_clean()
    text = store.close_bundle('2026-10')
    evidence = store.find_close('2026-10')['evidence_sha256']
    again = store.close_period('2026-10', base['run_id'], 2)
    assert again['evidence_sha256'] == evidence
    assert store.close_bundle('2026-10') == text


def test_failed_close_keeps_previous_close(clean_rows):
    base = close_clean()
    text = store.close_bundle('2026-10')
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    dirty = store.save(*_pair(run(csv_bytes(rows + [late]), {'period': '2026-10'})))
    with pytest.raises(DomainError):
        store.close_period('2026-10', dirty['run_id'], dirty['review_version'])
    # 失败不改动已封账证据。
    assert store.close_bundle('2026-10') == text


def test_export_shows_adopted_versions_and_deferred(clean_rows):
    from app.modules.m7_archive.export_out import bundle
    close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    result = run(csv_bytes(rows + [late]), {'period': '2026-10'})
    text = bundle(result)
    assert '已复核冻结' in text and '延后' in text and 'PERIOD_CLOSED' in text


def test_cross_month_requires_period_and_foreign_rows_rejected():
    rows = list(csv.DictReader(io.StringIO(load('clean.csv').decode('utf-8-sig'))))
    extra = dict(rows[0]); extra.update(id='00100', employee='E100', shift_day='2026-11-01',
                                        at='2026-11-01T22:00:00+08:00')
    # 同批混入两个归属月而未指定周期：拒绝，避免补卡记错期。
    with pytest.raises(DomainError) as error:
        run(csv_bytes(rows + [extra]), {})
    assert error.value.code == 'PERIOD'
    # 指定 2026-11 后，未在延后账内的 2026-10 行仍不得计入本期。
    with pytest.raises(DomainError) as error:
        run(csv_bytes(rows + [extra]), {'period': '2026-11'})
    assert error.value.code == 'PERIOD'
    state = run(csv_bytes([extra]), {'period': '2026-11'})
    assert state['period'] == '2026-11'


def test_other_period_rows_are_deferred_not_lost(clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    sep = dict(rows[0]); sep.update(id='S001', employee='E500', shift_day='2026-09-01',
                                    at='2026-09-01T22:00:00+08:00')
    result = run(csv_bytes(rows + [sep]), {'period': '2026-10'})
    entry = next(d for d in result['deferred'] if d['employee'] == 'E500')
    assert entry['reason'] == 'FOREIGN_PERIOD'
    assert entry['period'] == '2026-09' and entry['target_period'] == '2026-11'
    assert result['reconcile']['balanced'] is True


def test_older_deferred_version_does_not_displace_newer(clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    newer = dict(late); newer.update(version='3', at='2026-10-02T07:30:00+08:00')
    store.save(*_pair(run(csv_bytes(rows + [newer]), {'period': '2026-10'})))
    store.save(*_pair(run(csv_bytes(rows + [late]), {'period': '2026-10'})))
    ledger = store.deferral_ledger()
    assert len(ledger) == 1 and ledger[0]['version'] == '3'


def test_revision_of_adopted_card_is_deferred_not_lost(clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    store.save(*_pair(run(csv_bytes(rows + [late]), {'period': '2026-10'})))
    revised = dict(late); revised.update(version='3', at='2026-10-02T07:30:00+08:00')
    nxt = run(csv_bytes(next_month_rows(clean_rows) + [late, revised]), {'period': '2026-11'})
    entry = next(d for d in nxt['deferred'] if d['employee'] == 'E001')
    assert entry['reason'] == 'PERIOD_CLOSED'
    assert nxt['reconcile']['balanced'] is True


def test_page_shows_period_and_deferrals(client, clean_rows):
    close_clean()
    rows = [dict(r) for r in clean_rows]
    late = dict(rows[1]); late.update(id='00099', version='2', at='2026-10-02T07:00:00+08:00')
    client.post('/api/runs', json={'csv': csv_bytes(rows + [late]).decode(), 'parameters': {'period': '2026-10'}})
    page = client.get('/m3_versions').data.decode()
    assert '已封账' in page and '已复核冻结' in page and 'PERIOD_CLOSED' in page
    archive = client.get('/m7_archive').data.decode()
    assert '延后账' in archive and '封账 2026-10' in archive
