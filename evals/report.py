"""Dependency-free aggregate metrics and safely escaped static reports."""
from collections import Counter, defaultdict
from html import escape
import json
from math import ceil


def distribution(values, percentiles=True):
    values = sorted(values)
    if not values:
        return {'n': 0}
    result = {'n': len(values), 'min': values[0], 'max': values[-1], 'mean': sum(values) / len(values)}
    if percentiles:
        result['p50'] = values[ceil(len(values) * .5) - 1]
        if len(values) >= 100:
            result.update(p90=values[ceil(len(values) * .9) - 1], p95=values[ceil(len(values) * .95) - 1])
    return result


def summarize(results, live):
    counts = {s: sum(r['status'] == s for r in results)
              for s in ('pass', 'fail', 'error', 'insufficient_evidence', 'unscored')}
    criteria = defaultdict(Counter)
    categories = defaultdict(Counter)
    scenarios = defaultdict(list)
    for row in results:
        scenarios[row['id']].append(row['status'])
        for category in row['case']['tags']:
            categories[category][row['status']] += 1
        for check in row['checks']:
            criteria[check['criterion']][check['status']] += 1
    quality = {}
    for key, count in criteria.items():
        valid = count['pass'] + count['fail']
        quality[key] = {**dict(count), 'valid': valid, 'pass_rate': count['pass'] / valid if valid else None}
    metrics = {}
    for key in ('replyer_ms', 'judge_ms', 'duration_ms'):
        # Include failed attempts; report their count separately, not silently drop them.
        metrics[key] = distribution([r[key] for r in results if key in r], percentiles=live)
    usage = {}
    for stage in ('replyer', 'judge'):
        reports = ([e.get('usage') for r in results for e in r['exchanges']] if stage == 'replyer'
                   else [r.get('judge_usage') for r in results])
        reports = [r for r in reports if isinstance(r, dict)]
        usage[stage] = {'reported_calls': len(reports), 'input_total': sum(r.get('input', 0) for r in reports),
                        'output_total': sum(r.get('output', 0) for r in reports)}
    return {'total': len(results), 'scenario_count': len(scenarios), 'counts': counts, 'criteria': quality,
            'categories': dict(categories), 'latency': metrics, 'usage': usage,
            'scenarios_with_failure': sum('fail' in states for states in scenarios.values()),
            'all_repeats_pass': sum(all(s == 'pass' for s in states) for states in scenarios.values()),
            'error_stages': dict(Counter(r.get('error_stage', 'unknown') for r in results if r['status'] == 'error'))}


def paired_comparison(results, previous):
    old = {(r['id'], r.get('repetition', 1)): r for r in previous}
    pairs = []
    for row in results:
        baseline = old.get((row['id'], row.get('repetition', 1)))
        if not baseline:
            continue
        compatible = baseline['case'] == row['case'] and bool(baseline.get('judge_output')) == bool(row.get('judge_output'))
        pairs.append({'id': row['id'], 'repetition': row.get('repetition', 1), 'compatible': compatible,
                      'regression': compatible and baseline['status'] == 'pass' and row['status'] == 'fail',
                      'before': baseline.get('reply'), 'after': row.get('reply'),
                      'status_before': baseline['status'], 'status_after': row['status']})
    return pairs


def render_report(path, manifest, summary, results, previous=None):
    def block(value):
        return '<pre>' + escape(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'

    details = []
    for row in sorted(results, key=lambda r: (r['status'] not in {'fail', 'error'}, r['id'])):
        details.append('<details><summary>' + escape(f"{row['id']} #{row.get('repetition', 1)} · {row['status']}")
                       + '</summary>' + block({k: row.get(k) for k in ('case', 'reply', 'checks', 'error_type', 'error_stage')}) + '</details>')
    comparison = ''
    if previous is not None:
        comparison = '<h2>基线配对对比</h2><p>按场景和重复序号配对。数据或评分模式不同标为不可比；更换 Judge 版本仍需人工确认。</p>' + block(paired_comparison(results, previous))
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Momoi · Interaction Eval</title><style>
:root{--ink:#18352e;--paper:#f5f1e7;--line:#c3cbbd}body{margin:0;color:var(--ink);background:radial-gradient(ellipse at top right,#dce9ce,transparent 65%),var(--paper);font-family:Georgia,"Songti SC",serif}main{max-width:1080px;margin:auto;padding:40px 22px}h1{font-size:clamp(32px,6vw,58px);letter-spacing:-1px}h2{margin-top:36px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 monospace;padding:18px;background:#ffffff90;border:1px solid var(--line);border-radius:8px}details{border-bottom:1px solid var(--line);padding:14px 0}summary{cursor:pointer}p{line-height:1.8}.label{letter-spacing:3px;font:12px monospace}</style><main>
<p class="label">MOMOI / SYNTHETIC INTERACTION LAB</p><h1>交互评估报告</h1>
<p>仅使用虚构场景。未评分不等于通过；执行错误、Judge 错误和证据不足分别报告。无真实渠道投递，无法测量实际收信延迟。</p>
'''
    html += '<h2>本次运行</h2>' + block(manifest) + '<h2>质量与稳定性</h2>' + block({k: v for k, v in summary.items() if k not in {'latency', 'usage'}})
    html += '<h2>性能与用量</h2><p>Replyer 时间为模型调用累计耗时，Judge 单独计时；整体时间包含数据库等开销。分位数使用 nearest-rank，真实运行满 100 条才展示 P90/P95；重复场景不是独立场景。烟测只报范围与均值。失败尝试计入耗时，未提供价格配置所以不估算费用。</p>' + block({'latency': summary['latency'], 'usage': summary['usage']})
    html += comparison + '<h2>场景详情 · 失败优先</h2>' + ''.join(details) + '</main></html>'
    path.write_text(html, encoding='utf-8')
