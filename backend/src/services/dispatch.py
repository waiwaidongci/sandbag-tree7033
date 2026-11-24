"""排车记录版本化：并发更正另存新版本，受理后按受理时的规则版本与名额参数重放。"""
import time
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import insert, select, update

from src.models.database import add_event, engine
from src.models.tables import datasets, dispatch_records, runs
from src.services.domain import NUMERIC_LIMITS, TIME_FORMAT, Rule, replay_verdicts


class DispatchError(ValueError):
    """业务校验失败，由路由转成 422。"""


@dataclass(frozen=True)
class DispatchContext:
    rules: dict          # bay -> Rule
    deadline: int
    miss_cost: int
    quota: int


def _load_run(connection, run_id):
    run = connection.execute(select(runs).where(runs.c.id == run_id)).mappings().one_or_none()
    if run is None:
        raise HTTPException(404, '分析任务不存在。')
    result = run['result']
    if not result or not result.get('selected_rules'):
        raise DispatchError('该分析任务尚无定型规则，无法建立排车记录。')
    dataset = connection.execute(
        select(datasets).where(datasets.c.id == run['dataset_id'])).mappings().one_or_none()
    if dataset is None:
        raise DispatchError('分析任务绑定的数据版本不存在，无法核对批次特征。')
    return run, result, dataset['records']


def _rules_from_snapshot(snapshot):
    rules = {}
    for item in snapshot:
        rules[item['bay']] = Rule(
            bay=item['bay'], key=item['key'], train_cost=item.get('train_cost', 0),
            threshold=item.get('threshold'),
            left_alert=bool(item.get('left_alert', False)),
            right_alert=bool(item.get('right_alert', False)),
        )
    return rules


def _validate_payload(payload, bays, deadline):
    """校验排车明细并回填时间戳与超时标注（实耗只用于标注与计分）。"""
    if not isinstance(payload, list) or not payload:
        raise DispatchError('排车明细至少需要一条批次。')
    required = {'batch_id', 'loading_bay', 'planned_at', 'elapsed_min'}
    seen = set()
    normalized = []
    low, high = NUMERIC_LIMITS['elapsed_min']
    for index, row in enumerate(payload, start=1):
        if not isinstance(row, dict) or not required.issubset(row):
            raise DispatchError(f'第 {index} 条排车明细字段不完整，需包含 {sorted(required)}。')
        batch_id = str(row['batch_id']).strip()
        bay = str(row['loading_bay']).strip()
        if not batch_id or not bay:
            raise DispatchError(f'第 {index} 条排车明细缺少批次编号或装车位。')
        if bay not in bays:
            raise DispatchError(f'批次 {batch_id} 的装车位 {bay} 不在定型规则覆盖范围内。')
        if batch_id in seen:
            raise DispatchError(f'批次 {batch_id} 在同一份排车记录中重复。')
        seen.add(batch_id)
        planned_at = row['planned_at']
        try:
            timestamp = datetime.strptime(planned_at, TIME_FORMAT)
            if timestamp.strftime(TIME_FORMAT) != planned_at:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise DispatchError(f'批次 {batch_id} 的排车时间无效，请用本地时间 YYYY-MM-DD HH:MM。') from error
        elapsed = row.get('elapsed_min')
        if type(elapsed) is not int or not low <= elapsed <= high:
            raise DispatchError(f'批次 {batch_id} 的实耗分钟无效，请填写 {low} 至 {high} 的整数。')
        normalized.append({
            # 仅存 JSON 可序列化字段；_ts 在重放时由 planned_at 重新解析
            'batch_id': batch_id, 'loading_bay': bay, 'planned_at': planned_at,
            'elapsed_min': elapsed, 'overdue': elapsed > deadline,
        })
    return normalized


def _with_timestamps(rows):
    return [{**row, '_ts': datetime.strptime(row['planned_at'], TIME_FORMAT)} for row in rows]


def _freeze_verdicts(rows, rules, source_index):
    """按受理时的规则版本，为每个批次冻结规则命中结论与判断路径。

    班前特征取自分析任务绑定的数据版本（受理时点的版本），排车单只携带
    批次号、装车位、叫车时间和实耗。
    """
    frozen = []
    for row in rows:
        rule = rules.get(row['loading_bay'])
        if rule is None:
            raise DispatchError(f"批次 {row['batch_id']} 的装车位 {row['loading_bay']} 缺少定型规则。")
        source = source_index.get((row['batch_id'], row['loading_bay']))
        if source is None:
            raise DispatchError(
                f"批次 {row['batch_id']} 不在数据版本台账中，无法按定型规则核对班前特征。")
        verdict_row = {**row, **{key: source[key] for key in source if key not in row}}
        frozen.append({**row, 'wants_alert': rule.wants_alert(verdict_row),
                       'trace': rule.trace(verdict_row)})
    return frozen


def create_dispatch(body, actor):
    now = time.time()
    dispatch_no = body.dispatch_no.strip()
    if len(dispatch_no) < 2 or len(dispatch_no) > 60:
        raise DispatchError('排车单号须为 2 至 60 个字符。')
    with engine.begin() as connection:
        run, result, _ = _load_run(connection, body.run_id)
        options = run['options']
        bays = result['bays']
        payload = _validate_payload(body.payload, bays, options['deadline_min'])
        existing = connection.execute(
            select(dispatch_records).where(dispatch_records.c.dispatch_no == dispatch_no)
        ).mappings().all()
        if any(row['state'] == 'draft' for row in existing):
            raise DispatchError(f'排车单 {dispatch_no} 已有草稿，请在原记录上更正或受理。')
        version = max((row['version'] for row in existing), default=0) + 1
        parent_id = max((row['id'] for row in existing), default=None)
        key = connection.execute(insert(dispatch_records).values(
            dispatch_no=dispatch_no, version=version, parent_id=parent_id, run_id=body.run_id,
            state='draft', rule_snapshot=result['selected_rules'], options_snapshot=dict(options),
            payload=payload, owner=actor, created=now)).inserted_primary_key[0]
        add_event(connection, actor, 'dispatch_create', key, now)
    return {'id': key, 'dispatch_no': dispatch_no, 'version': version, 'state': 'draft'}


def correct_dispatch(key, body, actor):
    """并发更正：不覆盖原记录，而是在同一单号下另存一条新草稿版本。"""
    now = time.time()
    with engine.begin() as connection:
        current = connection.execute(
            select(dispatch_records).where(dispatch_records.c.id == key)
        ).mappings().one_or_none()
        if current is None:
            raise HTTPException(404, '排车记录不存在。')
        # 已受理/已完成/已替代的记录不可改写，只能作为新版本的父记录另存；草稿可直接更正。
        run, result, _ = _load_run(connection, current['run_id'])
        options = run['options']
        payload = _validate_payload(body.payload, result['bays'], options['deadline_min'])
        siblings = connection.execute(
            select(dispatch_records).where(dispatch_records.c.dispatch_no == current['dispatch_no'])
        ).mappings().all()
        if any(row['state'] == 'draft' and row['id'] != key for row in siblings):
            raise DispatchError('该排车单存在另一条草稿，请先受理或处理后再更正。')
        next_version = max(row['version'] for row in siblings) + 1
        # 乐观并发：草稿须仍为读取时状态才允许置为已替代；其余状态保留原样，只新增版本
        if current['state'] == 'draft':
            guard = connection.execute(update(dispatch_records).where(
                dispatch_records.c.id == key,
                dispatch_records.c.state == 'draft').values(state='superseded'))
            if guard.rowcount != 1:
                raise DispatchError('记录已被其他操作员更新，请刷新后基于最新版本更正。')
        elif current['state'] != 'accepted' and current['state'] != 'finished':
            raise DispatchError('已被替代的记录不能再次更正，请基于最新版本操作。')
        new_key = connection.execute(insert(dispatch_records).values(
            dispatch_no=current['dispatch_no'], version=next_version, parent_id=key,
            run_id=current['run_id'], state='draft', rule_snapshot=current['rule_snapshot'],
            options_snapshot=current['options_snapshot'], payload=payload, owner=actor, created=now)
        ).inserted_primary_key[0]
        add_event(connection, actor, 'dispatch_correct', new_key, now)
    return {'id': new_key, 'dispatch_no': current['dispatch_no'], 'version': next_version, 'state': 'draft'}


def _context(record):
    options = record['options_snapshot']
    return DispatchContext(_rules_from_snapshot(record['rule_snapshot']),
                           options['deadline_min'], options['miss_cost'], options['alert_quota'])


def _replay_record(connection, record):
    """按受理时冻结的规则版本与名额参数重放；恢复时调用结果必须与受理时一致。"""
    context = _context(record)
    run = connection.execute(select(runs).where(runs.c.id == record['run_id'])).mappings().one()
    dataset = connection.execute(
        select(datasets).where(datasets.c.id == run['dataset_id'])).mappings().one()
    source_index = {(item['batch_id'], item['loading_bay']): item for item in dataset['records']}
    verdicts = _freeze_verdicts(_with_timestamps(record['payload']), context.rules, source_index)
    totals, details = replay_verdicts(verdicts, context.quota, context.miss_cost)
    return context, totals, details


def accept_dispatch(key, actor):
    """受理时冻结规则版本、逐批命中结论与名额参数；中断恢复仍按此快照重放。"""
    now = time.time()
    with engine.begin() as connection:
        record = connection.execute(
            select(dispatch_records).where(dispatch_records.c.id == key)
        ).mappings().one_or_none()
        if record is None:
            raise HTTPException(404, '排车记录不存在。')
        if record['state'] != 'draft':
            raise DispatchError('仅草稿状态的排车记录可以受理。')
        _, totals, details = _replay_record(connection, record)
        updated = connection.execute(update(dispatch_records).where(
            dispatch_records.c.id == key, dispatch_records.c.state == 'draft').values(
            state='accepted', accepted=now, replay={'totals': totals, 'details': details}))
        if updated.rowcount != 1:
            raise DispatchError('记录状态已变化，请刷新后重试。')
        add_event(connection, actor, 'dispatch_accept', key, now)
    return {'id': key, 'state': 'accepted', 'totals': totals}


def finish_dispatch(key, actor):
    """完装签收：实耗已在受理快照内计分，完成动作只锁定记录，不改变分数。"""
    now = time.time()
    with engine.begin() as connection:
        record = connection.execute(
            select(dispatch_records).where(dispatch_records.c.id == key)
        ).mappings().one_or_none()
        if record is None:
            raise HTTPException(404, '排车记录不存在。')
        if record['state'] != 'accepted':
            raise DispatchError('仅已受理的排车记录可以完成签收。')
        updated = connection.execute(update(dispatch_records).where(
            dispatch_records.c.id == key, dispatch_records.c.state == 'accepted').values(
            state='finished', finished=now))
        if updated.rowcount != 1:
            raise DispatchError('记录状态已变化，请刷新后重试。')
        add_event(connection, actor, 'dispatch_finish', key, now)
    return {'id': key, 'state': 'finished'}


def resume_dispatch(key):
    """中断恢复：按受理时冻结的规则版本和参数重新回放，结果与受理时一致。"""
    with engine.connect() as connection:
        record = connection.execute(
            select(dispatch_records).where(dispatch_records.c.id == key)
        ).mappings().one_or_none()
        if record is None:
            raise HTTPException(404, '排车记录不存在。')
        if record['state'] not in ('accepted', 'finished'):
            raise DispatchError('仅已受理的记录可以恢复重放。')
        _, totals, details = _replay_record(connection, record)
        frozen = record['replay']
        comparable = [{k: v for k, v in item.items() if k != '_ts'} for item in details]
        frozen_comparable = [{k: v for k, v in item.items() if k != '_ts'} for item in frozen['details']]
        consistent = frozen['totals'] == totals and frozen_comparable == comparable
    return {
        'id': key, 'dispatch_no': record['dispatch_no'], 'version': record['version'],
        'state': record['state'], 'frozen_totals': frozen['totals'],
        'replay_totals': totals, 'consistent': consistent,
        'options_snapshot': record['options_snapshot'], 'rule_snapshot': record['rule_snapshot'],
        'details': details,
    }


def list_dispatch(dispatch_no=None):
    with engine.connect() as connection:
        statement = select(dispatch_records).order_by(dispatch_records.c.id.desc()).limit(100)
        rows = [dict(row) for row in connection.execute(statement).mappings()]
    for row in rows:
        row['payload'] = [{'batch_id': item['batch_id'], 'loading_bay': item['loading_bay'],
                           'planned_at': item['planned_at'], 'elapsed_min': item['elapsed_min']}
                          for item in row['payload']]
        if row['replay']:
            row['replay'] = {'totals': row['replay']['totals']}
    if dispatch_no:
        rows = [row for row in rows if row['dispatch_no'] == dispatch_no]
    return rows
