from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime


FEATURES = {
    'planned_bags': ('计划砂袋数', '袋', '>', '将大批次拆成可独立叫号的装车波次，按原发运顺序交接。'),
    'scheduled_loaders': ('排班装载员', '人', '≤', '开装前由班长确认实到人员，从同班机动岗补足排班缺口。'),
    'ready_pct': ('预备区到位率', '%', '≤', '先核清缺袋，再由预备区班组补足同批砂袋、核对托盘签，复测到位率后叫车。'),
    'queued_trucks': ('待装车辆', '辆', '>', '按空闲装车位分批叫号，下一车在候车区等待领班确认。'),
}
FEATURE_ORDER = tuple(FEATURES)
# 每个装车位候选规则的固定顺序：停用在前，随后按特征列顺序，同分组合按此顺序取首项。
OFF_KEY = 'off'
NUMERIC_LIMITS = {
    'planned_bags': (1, 10000), 'scheduled_loaders': (1, 100),
    'ready_pct': (0, 100), 'queued_trucks': (0, 100), 'elapsed_min': (1, 1440),
}
TIME_FORMAT = '%Y-%m-%d %H:%M'
WINDOW_SECONDS = 3600  # 叫车计入本批及往前不足一小时的预警


@dataclass(frozen=True)
class Leaf:
    total: int
    overdue: int
    alert: bool

    def loss(self, miss_cost):
        return self.total - self.overdue if self.alert else self.overdue * miss_cost


@dataclass(frozen=True)
class Rule:
    """单条件预警规则；key 为 off 表示该装车位停用，全部转常规。"""
    bay: str
    key: str
    train_cost: int
    threshold: float | None = None
    left_alert: bool = False
    right_alert: bool = False

    @property
    def disabled(self):
        return self.key == OFF_KEY

    @property
    def label(self):
        return '停用' if self.disabled else FEATURES[self.key][0]

    def summary(self):
        if self.disabled:
            return f'{self.bay}停用（本车位全部转常规）'
        feature_label, unit, _, _ = FEATURES[self.key]
        return (
            f'{self.bay} · {feature_label} ≤ {self.threshold:g} {unit} → '
            f'{"预警" if self.left_alert else "常规"}；'
            f'{feature_label} > {self.threshold:g} {unit} → {"预警" if self.right_alert else "常规"}'
        )

    def wants_alert(self, row):
        if self.disabled:
            return False
        side = int(row[self.key] > self.threshold)
        return self.right_alert if side else self.left_alert

    def trace(self, row):
        if self.disabled:
            return self.summary() + '（不发预警）'
        feature_label, unit, _, _ = FEATURES[self.key]
        side = int(row[self.key] > self.threshold)
        mark = '>' if side else '≤'
        return f'{self.bay} · {feature_label} {mark} {self.threshold:g} {unit}（实测 {row[self.key]} {unit}）'


def sample_rows() -> list[dict]:
    rows = []
    readiness = (48, 54, 60, 65, 69, 73, 76, 79, 83, 85, 87, 90, 92, 94, 97, 100)
    for day in range(8):
        date = f'2026-11-{17 + day:02d}'
        for slot, ready in enumerate(readiness):
            # The simulated early dispatch plan assigns large batches during high pressure.
            # On the last two days, planned load size rotates independently of pressure.
            high_pressure = slot < 8 and slot != 2 + day % 4
            high_pressure |= day in (2, 5, 7) and slot == (14 if day == 7 else 15)
            if day < 6:
                planned_bags = (340 if high_pressure else 190) + 5 * (slot % 5)
            else:
                planned_bags = 180 + 12 * ((slot * 5 + day * 3) % 16)
            rows.append({
                'batch_id': f'SD-{17 + day:02d}-{slot + 1:02d}',
                'planned_at': f'{date} {7 + slot // 2:02d}:{30 * (slot % 2):02d}',
                'loading_bay': ('东闸一号位', '东闸二号位')[slot % 2],
                'planned_bags': planned_bags,
                'scheduled_loaders': 3 + (slot * 3 + day) % 4,
                'ready_pct': ready,
                'queued_trucks': (slot * 3 + day * 2 + slot // 4) % 7,
                'elapsed_min': (40 if high_pressure else 24) + (slot + day) % 9,
            })
    return rows


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _parse_time(rows):
    groups = defaultdict(list)
    seen = set()
    timed = []
    for index, row in enumerate(rows, start=1):
        batch = row.get('batch_id')
        bay = row.get('loading_bay')
        if not isinstance(batch, str) or not batch.strip() or not isinstance(bay, str) or not bay.strip():
            raise ValueError(f'第 {index} 条记录缺少批次编号或装车位，请补齐台账后重导。')
        if batch.strip() in seen:
            raise ValueError(f'批次 {batch} 重复，请核对交接单并保留每个批次的一条有效记录。')
        seen.add(batch.strip())
        planned_at = row.get('planned_at')
        try:
            timestamp = datetime.strptime(planned_at, TIME_FORMAT)
            if timestamp.strftime(TIME_FORMAT) != planned_at:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ValueError(f'批次 {batch} 的排车时间无效，请用本地时间 YYYY-MM-DD HH:MM。') from error
        for name, (low, high) in NUMERIC_LIMITS.items():
            if not _integer(row.get(name), low, high):
                label = FEATURES[name][0] if name in FEATURES else '实耗分钟'
                raise ValueError(f'批次 {batch} 的{label}无效，请填写 {low} 至 {high} 的整数。')
        enriched = {**row, 'loading_bay': bay.strip(), 'batch_id': batch.strip(),
                    '_ts': timestamp, 'overdue': row['elapsed_min'] > row['deadline_min']}
        groups[timestamp.date().isoformat()].append(enriched)
        timed.append(enriched)
    if len({row['loading_bay'] for row in timed}) < 2:
        raise ValueError('联合比选需要两处装车位的台账，请补齐另一装车位的记录。')
    if len(groups) < 4:
        raise ValueError('至少需要四个不同排车日期，请补齐训练日、选择日和留出日的记录。')
    for date, day in groups.items():
        if len(day) < 4:
            raise ValueError(f'{date} 不足四个批次，请补齐当日台账后重跑。')
    days = [sorted(groups[date], key=lambda row: (row['planned_at'], row['batch_id']))
            for date in sorted(groups)]
    return days


def split_days(rows, deadline):
    """按本地排车日期切整日组：最晚日留出，倒数第二日选模型，其余日期拟合。"""
    days = _parse_time([{**row, 'deadline_min': deadline} for row in rows])
    train = [row for day in days[:-2] for row in day]
    return train, days[-2], days[-1], sorted({row['loading_bay'] for day in days for row in day})


def _require_both_outcomes(name, rows):
    if len({row['overdue'] for row in rows}) < 2:
        raise ValueError(f'{name}只有一种结果，请核对超时线或补齐该期按时和超时批次。')


def _leaf(total, overdue, miss_cost):
    return Leaf(total, overdue, overdue * miss_cost > total - overdue)


def fit_bay_rules(bay, train, miss_cost):
    """单个装车位的候选：停用 + 每个特征各一条最优深度一单条件规则。

    阈值取相邻不同训练值的中点，每叶至少两批；同特征同训练代价保留更低阈值。
    """
    total = len(train)
    overdue = sum(row['overdue'] for row in train)
    candidates = [Rule(bay, OFF_KEY, overdue * miss_cost)]  # 停用：全部常规，超时全部计漏报
    for feature in FEATURE_ORDER:
        ordered = sorted(train, key=lambda row: row[feature])
        left_overdue = 0
        best = None
        for left_total, (last_left, first_right) in enumerate(zip(ordered, ordered[1:]), start=1):
            left_overdue += last_left['overdue']
            right_total = total - left_total
            if min(left_total, right_total) < 2 or last_left[feature] == first_right[feature]:
                continue
            left = _leaf(left_total, left_overdue, miss_cost)
            right = _leaf(right_total, overdue - left_overdue, miss_cost)
            cost = left.loss(miss_cost) + right.loss(miss_cost)
            if best is None or cost < best.train_cost:  # 升序扫描：持平即更低阈值先入选
                threshold = (last_left[feature] + first_right[feature]) / 2
                best = Rule(bay, feature, cost, threshold, left.alert, right.alert)
        if best is not None:
            candidates.append(best)
    return candidates


def replay_day(rules_by_bay, day_rows, quota, miss_cost):
    """按叫车时间与批次号顺序回放整日，两处装车位共用一个每小时名额窗口。

    每次叫车计入本批及往前不足一小时内已发出的预警；不足名额才发预警，
    满额则该批转常规。实耗只用于事后计分，不影响发预警过程。
    """
    sent = []  # (timestamp, row) 仅记录实际发出的预警
    details = []
    totals = {'false_alerts': 0, 'misses': 0, 'cost': 0, 'skipped': 0, 'sent': 0}
    for row in sorted(day_rows, key=lambda item: (item['_ts'], item['batch_id'])):
        rule = rules_by_bay[row['loading_bay']]
        wants = rule.wants_alert(row)
        window = [item for item in sent if (row['_ts'] - item[0]).total_seconds() < WINDOW_SECONDS]
        if wants and len(window) < quota:
            sent.append((row['_ts'], row))
            status, reason = '预警', '名额内发出'
            false_alert = int(not row['overdue'])
            miss = 0
            totals['sent'] += 1
        elif wants:
            status, reason = '满额转常规', f'近一小时已发 {len(window)} 条，名额 {quota} 已满'
            false_alert = 0
            miss = int(row['overdue'])
            totals['skipped'] += 1
        else:
            status, reason = '常规', '规则未命中' + ('（停用）' if rule.disabled else '')
            false_alert = 0
            miss = int(row['overdue'])
        cost = false_alert + miss * miss_cost
        totals['false_alerts'] += false_alert
        totals['misses'] += miss
        totals['cost'] += cost
        details.append({
            'batch_id': row['batch_id'], 'bay': row['loading_bay'], 'planned_at': row['planned_at'],
            'rule': rule.summary(), 'trace': rule.trace(row), 'status': status, 'reason': reason,
            'elapsed_min': row['elapsed_min'], 'overdue': row['overdue'],
            'window_alerts': len(window) + (1 if status == '预警' else 0),
            'false_alert': false_alert, 'miss': miss, 'cost': cost,
        })
    return totals, details


def replay_verdicts(verdict_rows, quota, miss_cost):
    """按受理时冻结的逐批规则结论回放（排车记录版本化使用，不依赖班前特征原值）。

    verdict_rows 每项含 batch_id/loading_bay/planned_at/_ts/elapsed_min/overdue/
    wants_alert/trace。名额窗口、满额转常规与计分口径与 replay_day 完全一致。
    """
    sent = []
    details = []
    totals = {'false_alerts': 0, 'misses': 0, 'cost': 0, 'skipped': 0, 'sent': 0}
    for row in sorted(verdict_rows, key=lambda item: (item['_ts'], item['batch_id'])):
        wants = bool(row['wants_alert'])
        window = [item for item in sent if (row['_ts'] - item[0]).total_seconds() < WINDOW_SECONDS]
        if wants and len(window) < quota:
            sent.append((row['_ts'], row))
            status, reason = '预警', '名额内发出'
            false_alert = int(not row['overdue'])
            miss = 0
            totals['sent'] += 1
        elif wants:
            status, reason = '满额转常规', f'近一小时已发 {len(window)} 条，名额 {quota} 已满'
            false_alert = 0
            miss = int(row['overdue'])
            totals['skipped'] += 1
        else:
            status, reason = '常规', '规则未命中'
            false_alert = 0
            miss = int(row['overdue'])
        cost = false_alert + miss * miss_cost
        totals['false_alerts'] += false_alert
        totals['misses'] += miss
        totals['cost'] += cost
        details.append({
            'batch_id': row['batch_id'], 'bay': row['loading_bay'], 'planned_at': row['planned_at'],
            'trace': row['trace'], 'status': status, 'reason': reason,
            'elapsed_min': row['elapsed_min'], 'overdue': row['overdue'],
            'window_alerts': len(window) + (1 if status == '预警' else 0),
            'false_alert': false_alert, 'miss': miss, 'cost': cost,
        })
    return totals, details


def _record(section, subject, path, result, count, overdue, errors=None):
    false_alerts, misses, cost = errors if errors is not None else (None, None, None)
    return {
        '记录类型': section, '对象': subject, '判断路径': path, '结论': result,
        '批次数': count, '实超时批次': overdue, '误报': false_alerts, '漏报': misses, '代价': cost,
    }


def _constant_baseline_score(day_rows, miss_cost):
    """常量基线（预先确定的对照）：全批次常量预警，按逐批口径计分，不走名额抑制。"""
    false_alerts = sum(not row['overdue'] for row in day_rows)
    return {'false_alerts': false_alerts, 'misses': 0, 'cost': false_alerts,
            'skipped': 0, 'sent': len(day_rows)}


def _standalone_score(rule, rows, miss_cost):
    """不考虑名额时，单条规则在一批记录上的原始误报/漏报代价（用于训练叶展示）。"""
    false_alerts = misses = 0
    for row in rows:
        alert = rule.wants_alert(row)
        false_alerts += int(alert and not row['overdue'])
        misses += int((not alert) and row['overdue'])
    return false_alerts, misses, false_alerts + misses * miss_cost


def compute(rows: list[dict], options: dict) -> dict:
    deadline = options.get('deadline_min', 35)
    miss_cost = options.get('miss_cost', 4)
    quota = options.get('alert_quota', 3)
    if not _integer(deadline, 15, 90):
        raise ValueError('超时线须为 15 至 90 的整数分钟，请核对装车交接约定。')
    if not _integer(miss_cost, 1, 20):
        raise ValueError('漏报代价须为 1 至 20 的整数，请按误报为 1 的口径填写。')
    if not _integer(quota, 1, 50):
        raise ValueError('每小时预警名额须为 1 至 50 的整数，请按补袋班可承接的并发量填写。')

    train, validation, holdout, bays = split_days(rows, deadline)
    _require_both_outcomes('训练期', train)
    _require_both_outcomes('选择日', validation)
    _require_both_outcomes('留出日', holdout)
    for bay in bays:
        bay_train = [row for row in train if row['loading_bay'] == bay]
        if len(bay_train) < 4:
            raise ValueError(f'{bay} 训练批次不足四批，无法拟合单条件规则，请补齐该装车位台账。')
        _require_both_outcomes(f'{bay}训练期', bay_train)

    # 每装车位候选（停用 + 每特征一条中点规则），固定顺序用于同分裁决。
    candidates_by_bay = {bay: fit_bay_rules(bay, [r for r in train if r['loading_bay'] == bay], miss_cost)
                         for bay in bays}

    # 选择日：枚举两处装车位候选的全部可行组合，按共享名额回放取总代价最低。
    combos = []
    first_bay, second_bay = bays[0], bays[1]
    for index_a, rule_a in enumerate(candidates_by_bay[first_bay]):
        for index_b, rule_b in enumerate(candidates_by_bay[second_bay]):
            totals, _ = replay_day({first_bay: rule_a, second_bay: rule_b}, validation, quota, miss_cost)
            combos.append({
                'rules': {first_bay: rule_a, second_bay: rule_b},
                'order': (index_a, index_b), 'totals': totals,
            })
    chosen = min(combos, key=lambda item: (item['totals']['cost'], item['order']))
    chosen_rules = chosen['rules']
    validation_totals = chosen['totals']

    # 留出日：沿用选择日定型的规则与同一名额参数，按叫车时间和批次号回放。
    holdout_totals, holdout_details = replay_day(chosen_rules, holdout, quota, miss_cost)
    baseline_totals = _constant_baseline_score(holdout, miss_cost)
    holdout_gain = baseline_totals['cost'] - holdout_totals['cost']
    validation_baseline = _constant_baseline_score(validation, miss_cost)

    details = []
    # 选择日组合比选（全部可行组合，固定顺序同分裁决可复核）
    for rank, combo in enumerate(sorted(combos, key=lambda item: (item['totals']['cost'], item['order'])), start=1):
        rule_a, rule_b = combo['rules'][first_bay], combo['rules'][second_bay]
        totals = combo['totals']
        details.append(_record(
            '选择日组合', f'#{rank} {first_bay}：{rule_a.label} / {second_bay}：{rule_b.label}',
            f'{rule_a.summary()} ‖ {rule_b.summary()}',
            '入选' if combo is chosen else '备选', len(validation),
            sum(row['overdue'] for row in validation),
            (totals['false_alerts'], totals['misses'], totals['cost']),
        ))
    # 各装车位候选规则与训练叶
    for bay in bays:
        for rule in candidates_by_bay[bay]:
            bay_train = [row for row in train if row['loading_bay'] == bay]
            score = _standalone_score(rule, bay_train, miss_cost)
            details.append(_record(
                '候选规则', f'{bay} · {rule.label}', rule.summary(),
                '入选' if chosen_rules[bay] is rule else '候选',
                len(bay_train), sum(row['overdue'] for row in bay_train), score,
            ))
    # 留出日汇总（入选组合与常量基线）
    details.append(_record(
        '留出日汇总', '入选组合', ' ‖ '.join(rule.summary() for rule in chosen_rules.values()),
        '入选规则', len(holdout), sum(row['overdue'] for row in holdout),
        (holdout_totals['false_alerts'], holdout_totals['misses'], holdout_totals['cost']),
    ))
    details.append(_record(
        '留出日汇总', '常量基线', '两处装车位全批次常量预警（预先确定的逐批对照，不占名额）', '留出对照',
        len(holdout), sum(row['overdue'] for row in holdout),
        (baseline_totals['false_alerts'], baseline_totals['misses'], baseline_totals['cost']),
    ))
    # 因满额跳过的批次
    skipped = [item for item in holdout_details if item['status'] == '满额转常规']
    for item in skipped:
        details.append(_record(
            '满额跳过批次', f"{item['batch_id']} · {item['bay']} · {item['planned_at']}",
            item['trace'], item['reason'], 1, int(item['overdue']),
            (item['false_alert'], item['miss'], item['cost']),
        ))
    # 留出批次逐条判断路径与实耗（实耗只计分）
    for item in holdout_details:
        details.append(_record(
            '留出批次', f"{item['batch_id']} · {item['bay']} · {item['planned_at']}", item['trace'],
            f"{item['status']} / 实耗 {item['elapsed_min']} 分钟（{'超时' if item['overdue'] else '按时'}）",
            1, int(item['overdue']), (item['false_alert'], item['miss'], item['cost']),
        ))

    recommendation = _recommendation(chosen_rules, bays, holdout_gain, holdout_totals['skipped'])
    details.append(_record('流程建议', '下一班补袋班与装车班长',
                           ' ‖ '.join(rule.summary() for rule in chosen_rules.values()),
                           recommendation, None, None))

    train_first = train[0]['planned_at'][:10]
    train_last = train[-1]['planned_at'][:10]
    validation_date = validation[0]['planned_at'][:10]
    holdout_date = holdout[0]['planned_at'][:10]
    chosen_labels = '；'.join(f'{bay}选{chosen_rules[bay].label}' for bay in bays)
    headline = (f'{chosen_labels}。{holdout_date} 留出日代价 {holdout_totals["cost"]}，'
                f'常量基线 {baseline_totals["cost"]}，满额跳过 {holdout_totals["skipped"]} 批。')
    return {
        'headline': headline,
        'metrics': {
            '训练批次': len(train), '选择日批次': len(validation), '留出日批次': len(holdout),
            '每小时名额': quota,
            '入选选择日代价': validation_totals['cost'],
            '选择日基线代价': validation_baseline['cost'],
            '入选留出日代价': holdout_totals['cost'], '留出日基线代价': baseline_totals['cost'],
            '留出日误报': holdout_totals['false_alerts'], '留出日漏报': holdout_totals['misses'],
            '留出日发出预警': holdout_totals['sent'], '满额跳过批次': holdout_totals['skipped'],
        },
        'rows': details,
        'notes': [
            recommendation,
            f'本地排车日期整日隔离。{train_first} 至 {train_last} 分车位拟合，{validation_date} 联合比选两处装车位的'
            f'单条件规则，{holdout_date} 只按定型规则与同一名额参数做留出评估。',
            f'每装车位候选含「停用」与 {len(FEATURE_ORDER)} 个特征各自最优的深度一单条件规则；'
            '阈值取该车位相邻不同训练值的中点，每叶至少两批，同特征同训练代价取更低阈值。',
            f'选择日枚举两处车位候选的全部组合，按叫车时间回放：每次叫车计入本批及往前不足一小时（严格 <60 分钟）'
            f'内已发预警，两处合计不超过名额 {quota}；总代价最低者入选，同分按「停用优先、特征列顺序、'
            '一号位优先于二号位」的固定顺序裁决。',
            f'留出日沿用入选规则，按叫车时间和批次号发预警，满额批次转常规；误报 × 1 + 漏报 × {miss_cost} 计分，'
            '满额转常规仅在实际超时时计漏报。实耗严格超过 '
            f'{deadline} 分钟算超时，且只参与超时标注与计分，不参与发预警决策。',
            '演示结果来自模拟站点，流程试行后需用新的交班记录验证实际成效。',
        ],
        'chart': {'label': '选择日组合代价（前 10 组）', 'unit': '代价点', 'points': [
            {'label': f"{combo['rules'][first_bay].label}+{combo['rules'][second_bay].label}",
             'value': combo['totals']['cost']}
            for combo in sorted(combos, key=lambda item: (item['totals']['cost'], item['order']))[:10]
        ]},
        'can_publish': True,
        # 供排车记录版本化重放使用的定型规则（可 JSON 序列化）
        'selected_rules': [
            {'bay': bay, 'key': chosen_rules[bay].key, 'threshold': chosen_rules[bay].threshold,
             'left_alert': chosen_rules[bay].left_alert, 'right_alert': chosen_rules[bay].right_alert,
             'train_cost': chosen_rules[bay].train_cost, 'summary': chosen_rules[bay].summary()}
            for bay in bays
        ],
        'bays': bays,
    }


def _recommendation(chosen_rules, bays, holdout_gain, skipped):
    if holdout_gain <= 0:
        return ('留出日代价未低于常量基线，维持现行叫车顺序。由交接班长补记等待原因与实到人员，'
                '积累新班次后再复跑。')
    parts = []
    for bay in bays:
        rule = chosen_rules[bay]
        if rule.disabled:
            parts.append(f'{bay}停用单条件预警，全部按常规叫车')
            continue
        label, unit, _, action = FEATURES[rule.key]
        alert_path = '≤' if rule.left_alert else '>'
        parts.append(f'{bay}在叫车前检查{label} {alert_path} {rule.threshold:g} {unit}，命中时{action}')
    tail = f'近 {skipped} 批因满额转常规，需向补袋班说明并核对其处置记录。' if skipped else ''
    return '建议下一班试行。' + '；'.join(parts) + '。' + tail + '记录干预前后读数和完装分钟，由交接班复核。'
