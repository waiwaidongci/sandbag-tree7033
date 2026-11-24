from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime


FEATURES = {
    'planned_bags': ('计划砂袋数', '袋', '>', '将大批次拆成可独立叫号的装车波次，按原发运顺序交接。'),
    'scheduled_loaders': ('排班装载员', '人', '≤', '开装前由班长确认实到人员，从同班机动岗补足排班缺口。'),
    'ready_pct': ('预备区到位率', '%', '≤', '先核清缺袋，再由预备区班组补足同批砂袋、核对托盘签，复测到位率后叫车。'),
    'queued_trucks': ('待装车辆', '辆', '>', '按空闲装车位分批叫号，下一车在候车区等待领班确认。'),
}
SUBSETS = (
    ('常量基线', ()),
    ('装载工作量', ('planned_bags', 'scheduled_loaders')),
    ('场地准备', ('ready_pct', 'queued_trucks')),
    ('全部班前特征', tuple(FEATURES)),
)
NUMERIC_LIMITS = {
    'planned_bags': (1, 10000), 'scheduled_loaders': (1, 100),
    'ready_pct': (0, 100), 'queued_trucks': (0, 100), 'elapsed_min': (1, 1440),
}
TIME_FORMAT = '%Y-%m-%d %H:%M'


@dataclass(frozen=True)
class Leaf:
    total: int
    overdue: int
    alert: bool

    def loss(self, miss_cost):
        return self.total - self.overdue if self.alert else self.overdue * miss_cost


@dataclass(frozen=True)
class Stump:
    left: Leaf
    feature: str | None = None
    threshold: float | None = None
    right: Leaf | None = None

    def leaves(self):
        if self.right is None:
            return (('全批次', self.left),)
        label, unit, _, _ = FEATURES[self.feature]
        return (
            (f'{label} ≤ {self.threshold:g} {unit}', self.left),
            (f'{label} > {self.threshold:g} {unit}', self.right),
        )

    def branch_index(self, row):
        return int(self.feature is not None and row[self.feature] > self.threshold)

    def trace(self, row):
        path, leaf = self.leaves()[self.branch_index(row)]
        if self.feature is None:
            return path, leaf
        return f'{path}（实测 {row[self.feature]} {FEATURES[self.feature][1]}）', leaf


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


def _time_groups(rows, deadline):
    groups = defaultdict(list)
    seen = set()
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
        groups[timestamp.date().isoformat()].append({**row, 'overdue': row['elapsed_min'] > deadline})
    if len(groups) < 4:
        raise ValueError('至少需要四个不同排车日期，请补齐训练日、选择日和留出日的记录。')
    for date, day in groups.items():
        if len(day) < 4:
            raise ValueError(f'{date} 不足四个批次，请补齐当日台账后重跑。')
    days = [sorted(groups[date], key=lambda row: (row['planned_at'], row['batch_id'])) for date in sorted(groups)]
    train = [row for day in days[:-2] for row in day]
    return train, days[-2], days[-1]


def _require_both_outcomes(name, rows):
    if len({row['overdue'] for row in rows}) < 2:
        raise ValueError(f'{name}只有一种结果，请核对超时线或补齐该期按时和超时批次。')


def _leaf(total, overdue, miss_cost):
    return Leaf(total, overdue, overdue * miss_cost > total - overdue)


def _fit(train, allowed, miss_cost):
    total = len(train)
    overdue = sum(row['overdue'] for row in train)
    best = Stump(_leaf(total, overdue, miss_cost))
    best_cost = best.left.loss(miss_cost)
    for feature in allowed:
        ordered = sorted(train, key=lambda row: row[feature])
        left_overdue = 0
        for left_total, (last_left, first_right) in enumerate(zip(ordered, ordered[1:]), start=1):
            left_overdue += last_left['overdue']
            right_total = total - left_total
            if min(left_total, right_total) < 2 or last_left[feature] == first_right[feature]:
                continue
            left = _leaf(left_total, left_overdue, miss_cost)
            right = _leaf(right_total, overdue - left_overdue, miss_cost)
            cost = left.loss(miss_cost) + right.loss(miss_cost)
            if cost < best_cost:
                threshold = (last_left[feature] + first_right[feature]) / 2
                best = Stump(left, feature, threshold, right)
                best_cost = cost
    return best


def _score(model, rows, miss_cost):
    leaves = (model.left, model.right)
    alerts = [leaves[model.branch_index(row)].alert for row in rows]
    false_alerts = sum(alert and not row['overdue'] for alert, row in zip(alerts, rows))
    misses = sum(not alert and row['overdue'] for alert, row in zip(alerts, rows))
    return false_alerts, misses, false_alerts + misses * miss_cost


def _record(section, subject, path, result, count, overdue, errors=None):
    false_alerts, misses, cost = errors if errors is not None else (None, None, None)
    return {
        '记录类型': section, '对象': subject, '判断路径': path, '结论': result,
        '批次数': count, '实超时批次': overdue, '误报': false_alerts, '漏报': misses, '代价': cost,
    }


def _summary(model):
    return '；'.join(f'{path} → {"预警" if leaf.alert else "常规"}' for path, leaf in model.leaves())


def _recommendation(model, validation_gain, holdout_gain):
    if model.feature is None or validation_gain <= 0 or holdout_gain <= 0:
        return '维持现行装车顺序。由交接班长补记等待原因与实到人员，积累新班次后再复跑。'
    label, _, expected, action = FEATURES[model.feature]
    path, _ = model.leaves()[0 if model.left.alert else 1]
    if model.left.alert != (expected == '≤'):
        return f'预警分支为「{path}」。先由班长核对{label}的登记时点并抽查缺袋、排队或出勤记录，维持现行装车顺序。'
    return f'建议下一班试行。班长在叫车前检查「{path}」。命中时，{action}记录干预前后读数和完装分钟，由交接班复核。'


def compute(rows: list[dict], options: dict) -> dict:
    deadline = options.get('deadline_min', 35)
    miss_cost = options.get('miss_cost', 4)
    if not _integer(deadline, 15, 90):
        raise ValueError('超时线须为 15 至 90 的整数分钟，请核对装车交接约定。')
    if not _integer(miss_cost, 1, 20):
        raise ValueError('漏报代价须为 1 至 20 的整数，请按误报为 1 的口径填写。')
    train, validation, holdout = _time_groups(rows, deadline)
    _require_both_outcomes('训练期', train)
    _require_both_outcomes('选择日', validation)
    candidates = [
        (name, _fit(train, features, miss_cost)) for name, features in SUBSETS
    ]
    validation_scores = [_score(model, validation, miss_cost) for _, model in candidates]
    chosen = min(range(len(candidates)), key=lambda index: (validation_scores[index][2], index))
    name, model = candidates[chosen]
    baseline = candidates[0][1]
    _require_both_outcomes('留出日', holdout)
    held_baseline = _score(baseline, holdout, miss_cost)
    held_score = held_baseline if chosen == 0 else _score(model, holdout, miss_cost)
    validation_gain = validation_scores[0][2] - validation_scores[chosen][2]
    holdout_gain = held_baseline[2] - held_score[2]
    recommendation = _recommendation(model, validation_gain, holdout_gain)

    details = [
        _record('选择日比较', candidate, _summary(stump), '入选' if index == chosen else '备选',
                len(validation), sum(row['overdue'] for row in validation), validation_scores[index])
        for index, (candidate, stump) in enumerate(candidates)
    ]
    details.append(_record('留出日汇总', name, _summary(model), '入选模型', len(holdout),
                           sum(row['overdue'] for row in holdout), held_score))
    if chosen:
        details.append(_record('留出日汇总', candidates[0][0], _summary(baseline), '留出对照', len(holdout),
                               sum(row['overdue'] for row in holdout), held_baseline))
    details.extend(
        _record('训练叶', name, path, '预警' if leaf.alert else '常规', leaf.total, leaf.overdue)
        for path, leaf in model.leaves()
    )
    for row in holdout:
        path, leaf = model.trace(row)
        actual = row['overdue']
        false_alert = int(leaf.alert and not actual)
        miss = int(not leaf.alert and actual)
        details.append(_record(
            '留出批次', f"{row['batch_id']} · {row['loading_bay']} · {row['planned_at']}", path,
            f"{'预警' if leaf.alert else '常规'} / 实耗 {row['elapsed_min']} 分钟（{'超时' if actual else '按时'}）",
            1, int(actual), (false_alert, miss, false_alert + miss * miss_cost),
        ))
    details.append(_record('流程建议', '下一班班长', _summary(model), recommendation, None, None))

    train_first = train[0]['planned_at'][:10]
    train_last = train[-1]['planned_at'][:10]
    validation_date = validation[0]['planned_at'][:10]
    holdout_date = holdout[0]['planned_at'][:10]
    return {
        'headline': f'{name}入选，{holdout_date} 留出日的代价为 {held_score[2]}，常量基线为 {held_baseline[2]}。',
        'metrics': {
            '训练批次': len(train), '选择日批次': len(validation), '留出日批次': len(holdout),
            '入选选择日代价': validation_scores[chosen][2], '入选留出日代价': held_score[2],
            '留出日基线代价': held_baseline[2], '留出日误报': held_score[0], '留出日漏报': held_score[1],
        },
        'rows': details,
        'notes': [
            recommendation,
            f'本地排车日期整日隔离。{train_first} 至 {train_last} 拟合，{validation_date} 比较特征组，'
            f'{holdout_date} 只评估入选模型和预先确定的常量基线。选定后沿用训练规则。',
            f'实耗严格超过 {deadline} 分钟算超时。代价 = 误报 × 1 + 漏报 × {miss_cost}。'
            '叶内选择训练代价最低的预测，持平则输出常规。相邻训练值的中点可作阈值，每叶至少两批。'
            '同训练代价依次保留常量、特征列顺序、更低阈值。选择日持平时按表中组序取首项。',
            '装载实耗仅用于标注和回测。演示结果来自模拟站点，流程试行后需用新的交班记录验证实际成效。',
        ],
        'chart': {'label': '选择日各特征组代价', 'unit': '代价点', 'points': [
            {'label': candidate, 'value': score[2]}
            for (candidate, _), score in zip(candidates, validation_scores)
        ]},
        'can_publish': True,
    }
