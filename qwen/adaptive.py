"""Validated live tuning for unattended operation; QA standards stay unchanged."""
import json
from pathlib import Path

TUNING = Path.home() / '.local/share/strata-monitor/tuning.json'
DEFAULTS = {'thinking_budget': 16384, 'build_jobs': 4, 'session_jobs': 3,
            'max_rounds': 3, 'evidence_level': 0, 'lesson_categories': [],
            'prefer_verified': False}
LESSONS = {
    'regression': 'Before changing production code, run the regression on the original code and record the failing assertion. Then run the identical test after the fix. A test that passes both times is not regression proof.',
    'registration': 'Check test source registration paths against the actual files. For a manifest-only fix, use the registration gate as RED/GREEN evidence; a passing ctest alone does not prove the registry defect.',
    'rebuild': 'Build the affected test target before running ctest, and rebuild dependents of changed headers. Record the number of tests executed; zero tests is a failure.',
    'scope': 'Follow the actual user-visible path and edge case in the finding. Change only that behavior. If the contract is unclear, report NEEDS-CONTEXT with evidence rather than inventing a behavior change.',
    # 2026-10-20: was "fix the code if either [direct --check] fails" - both are red on the train on purpose, so fixers and
    # reviewers chased gate failures outside the change (about 40 of 67 bug reworks 10-18..20)
    'gates': 'After the final edit run `python3 ~/.local/share/zene-bunny-fix/qwen/feature_check.py <worktree>` and save its table and unpiped exit code. Complexity and file-length are red on the train on purpose: they fail only on items NEW in files you changed - fix the code for those, ignore the rest. Never change gate limits or baselines.',
}


def settings():
    try:
        value = json.loads(TUNING.read_text())
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    result = dict(DEFAULTS)
    bounds = {'thinking_budget': (4096, 16384), 'build_jobs': (1, 6),
              'session_jobs': (1, 4), 'max_rounds': (2, 4), 'evidence_level': (0, 2)}
    for key, (low, high) in bounds.items():
        n = value.get(key)
        if isinstance(n, int) and not isinstance(n, bool):
            result[key] = max(low, min(high, n))
    categories = value.get('lesson_categories', [])
    result['lesson_categories'] = [k for k in categories if k in LESSONS][:5] if isinstance(categories, list) else []
    result['prefer_verified'] = value.get('prefer_verified') is True
    return result


def order(items):
    if not settings()['prefer_verified']:
        return items
    # Reworks are selected separately; this orders untouched findings only.
    return sorted(items, key=lambda i: (
        not str(i.get('clef_tier', i.get('difficulty', ''))).endswith('easy'),
        i.get('realness') != 'likely_open_bug', bool(i.get('context_required'))))


def feedback():
    cfg = settings()
    lines = [LESSONS[k] for k in cfg['lesson_categories']]
    if cfg['evidence_level']:
        lines.insert(0, 'Work in concrete steps: inspect the smallest relevant function, run a regression, edit, rebuild, verify, report, commit. Do not spend a whole turn repeating a plan; use tools to resolve uncertainty.')
    if cfg['evidence_level'] >= 2:
        lines.append('The report must include an evidence table: exact RED command and exit code, exact GREEN command and exit code, affected-family test count, both gate exit codes, and the explicit files committed. If blocked, commit an honest BLOCKED report.')
    return '\n## Lessons from recent independent QA\n' + '\n'.join('- ' + line for line in lines) + '\n' if lines else ''


def request_budget(body):
    if not isinstance(body, dict) or not any(k in body for k in ('messages', 'input')):
        return body
    budget = settings()['thinking_budget']
    existing = body.get('reasoning_budget_tokens')
    if isinstance(existing, int) and existing > 0:
        budget = min(budget, existing)
    cap = body.get('max_completion_tokens') or body.get('max_tokens') or body.get('max_output_tokens')
    if isinstance(cap, int) and cap > 0:
        budget = min(budget, max(1, cap - min(2048, cap // 2)))
    return {**body, 'reasoning_budget_tokens': budget}
