#!/usr/bin/env python3
"""Free-only OpenCode sessions for the existing bug fixer and independent QA queue."""
import argparse
import datetime
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import time
import threading

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'OPENCODE-FREE.json'
STATE = ROOT / 'opencode-free-state.json'
CATALOG = ROOT / 'opencode-free-catalog.json'
CLI = str(Path.home() / '.local/bin/opencode')
# ALLOWED is the CATALOGUE ALLOWLIST - which Zen (opencode/*) ids the loop may run at all. What it DOES run is
# OPENCODE-FREE.json's "models". Owner, 2026-10-16 ("drop it down to just the best one of those in terms of completion
# rate ... just stick with muse, space bunny, mimo, and ll alpha"): four are active, the rest are parked here so
# re-enabling one is a single line in OPENCODE-FREE.json.
#   active: muse-spark-1.3-contributor-free, space-bunny-free, mimo-v2.6-flash-free, ll-alpha-free
ALLOWED = {'beam-free', 'muse-spark-1.3-contributor-free', 'space-bunny-free', 'mimo-v2.6-flash-free', 'll-alpha-free',
           # parked: dropped in the completion-rate/context pass, or barred as poor (the two Nemotrons)
           'big-pickle', 'deepseek-v4.1-flash-free', 'glm-5.3-flash-free', 'longcat-2.5-preview-free',
           'nemotron-3.5-lightning-free', 'nemotron-3.5-super-vl-free'}
# The one priced id the owner keeps (2026-10-16: "deepseek 4.1 flash can stay there, since it uses the least usage on
# the opencode go plan"). A PAID_PLAN id skips the Zen zero-cost catalogue check and is stopped by run_one() once its
# reported cost passes PAID_SESSION_CAP_USD - at 0.15/0.60 per Mtok that cap is effectively unreachable.
PAID_PLAN = {  # 2026-10-19 owner: Muse Spark contributor (paid) removed - 'get rid of muse contributor as a feature builder'
             'opencode-go/mimo-v2.6-flash', 'opencode-go/beam-free', 'opencode-go/deepseek-v4.1-flash', 'opencode-go/glm-5.3-flash', 'opencode-go/hy3', 'opencode-go/mimo-v2.6-pro', 'opencode-go/minimax-m3', 'opencode-go/qwen3.7-plus', 'opencode-go/qwen3.8-flash', 'opencode-go/space-bunny', 'opencode-go/minimax-m3.1-flash-free'}   # 2026-10-19: MiniMax M3.1 Flash, zero-cost on the Go route for a few days (owner)   # + the 2026-10-16 bug-fix trial models (BUG-TRIAL.json)   # owner 2026-10-16: "add glm 5.3 flash back"
PAID_SESSION_CAP_USD = 3.0   # owner 2026-10-22 "raise the cap": $1 cut long sessions once the time cap went (Q050-127 lost 149 min); both DeepSeek providers are flat-rate
LOCKS = ROOT / 'opencode-locks'      # one flock per model: concurrent workers never share a model
COOLDOWN = 900                       # a model that failed transiently rests this long; the others carry on
MAX_WORKERS = 12                      # upper bound on concurrent free sessions (the fixer sizes its pool from it)
# OpenCode retries a 429 ("Rate limit exceeded") silently and forever, so a rate-limited session just goes quiet.
FIRST_EVENT_S = 240                  # no event at all this long after the start: treat as rate-limited
QUIET_S = 1500                       # a later silence this long (longer than any build/test tool call): stalled
LIMITED_REST = 1800                  # a model that went quiet rests this long
# 2026-10-20: dsh's 'TRANSPORT: Connection error' (a network failure) counts too - it burned 4 rounds a job in seconds
TRANSIENT = re.compile(r'rate.?limit|429|too many|overload|503|502|504|500|quota|credit|billing|401|402|timed? ?out|unavailable|model.*not found|server error|UnknownError|internal error|connection error|TRANSPORT|ECONNRESET|ETIMEDOUT|fetch failed', re.I)


def full_id(model):
    return model if '/' in model else 'opencode/' + model


def slug(model):
    return model.replace('/', '__')


def write(path, value):
    tmp = path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')   # concurrent workers write too
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def load(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def eligible(model):
    cost = model.get('cost') or {}
    cache = cost.get('cache') or {}
    def zero(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0
    return (model.get('id') in ALLOWED and model.get('providerID') == 'opencode'
            and model.get('status') == 'active'
            and model.get('capabilities', {}).get('toolcall') is True
            and zero(cost.get('input')) and zero(cost.get('output'))
            and isinstance(cache, dict) and all(zero(value) for value in cache.values()))


def parse_catalog(text):
    result = {}
    decoder = json.JSONDecoder()
    for match in re.finditer(r'^opencode/([^\n]+)\n', text, re.M):
        # CLI metadata can include records in a non-JSON display format.
        # Ignore unrelated records; unverifiable candidates stay excluded.
        model_id = match.group(1).strip()
        if model_id not in ALLOWED:
            continue
        try:
            model, _ = decoder.raw_decode(text[match.end():].lstrip())
        except (ValueError, TypeError):
            continue
        if isinstance(model, dict) and model.get('id') == model_id and eligible(model):
            result[model['id']] = model
    return result


def catalog():
    old = load(CATALOG, {})
    if time.time() - old.get('checked_at', 0) < 3600:
        return old['models']
    result = subprocess.run([CLI, 'models', 'opencode', '--verbose', '--refresh', '--pure'],
                            capture_output=True, text=True, timeout=60, check=True)
    models = parse_catalog(result.stdout)
    response = subprocess.run(['curl', '-fsSL', '--connect-timeout', '10', '--max-time', '20',
                               'https://opencode.ai/zen/v1/models'],
                              capture_output=True, text=True, check=True)
    live = {x['id'] for x in json.loads(response.stdout)['data']}
    models = {key: value for key, value in models.items() if key in live}
    write(CATALOG, {'checked_at': time.time(), 'models': models})
    return models


def ready():
    cfg = load(CONFIG, {})
    state = load(STATE, {})
    return bool(cfg.get('enabled') and cfg.get('models') and state.get('paused_until', 0) <= time.time())


SCHEDULE = ROOT / 'MODEL-SCHEDULE.json'     # owner 2026-10-19: per-model blocked hours (DeepSeek peak), read live
DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _days(spec):
    out = set()
    for part in str(spec).split(','):
        part = part.strip()
        if '-' in part:
            a, b = (DAYS.index(x.strip()[:3].title()) for x in part.split('-', 1))
            out |= {d % 7 for d in range(a, a + (b - a) % 7 + 1)}
        elif part:
            out.add(DAYS.index(part[:3].title()))
    return out


def off_schedule(model, now=None):
    """The blocked window a model is in (or starts within lead_minutes), as 'Mon 01:00-04:00 UTC', else None.
    A window may cross midnight (from > to): it then belongs to the day it starts on."""
    import zoneinfo
    cfg = load(SCHEDULE, {}) or {}
    entry = (cfg.get('models') or {}).get(model)
    if not entry:
        return None
    lead = datetime.timedelta(minutes=float(cfg.get('lead_minutes') or 0))
    tz = zoneinfo.ZoneInfo(entry.get('tz') or 'UTC')
    t = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz)
    for w in entry.get('blocked') or []:
        days = _days(w.get('days', 'Mon-Sun'))
        fh, fm = map(int, w['from'].split(':'))
        th, tm = map(int, w['to'].split(':'))
        for back in (0, 1):                 # today's window, or yesterday's if it crosses midnight
            day = (t - datetime.timedelta(days=back)).date()
            if day.weekday() not in days:
                continue
            start = datetime.datetime(day.year, day.month, day.day, fh, fm, tzinfo=tz)
            end = datetime.datetime(day.year, day.month, day.day, th, tm, tzinfo=tz)
            if end <= start:
                end += datetime.timedelta(days=1)
            if start - lead <= t < end:
                return f"{DAYS[day.weekday()]} {w['from']}-{w['to']} {entry.get('tz') or 'UTC'}"
        # tomorrow's window starting within the lead time (e.g. 23:30 before a 00:00 window)
        nxt = (t + datetime.timedelta(days=1)).date()
        if nxt.weekday() in days:
            start = datetime.datetime(nxt.year, nxt.month, nxt.day, fh, fm, tzinfo=tz)
            if start - lead <= t < start:
                return f"{DAYS[nxt.weekday()]} {w['from']}-{w['to']} {entry.get('tz') or 'UTC'}"
    return None


def cooling(model):
    if off_schedule(model):
        return True
    try:
        return float((LOCKS / f'{slug(model)}.cooldown').read_text()) > time.time()
    except (FileNotFoundError, ValueError):
        return False


def rest(model, seconds=COOLDOWN):
    """Rest a model; a longer rest already recorded is kept."""
    LOCKS.mkdir(exist_ok=True)
    path = LOCKS / f'{slug(model)}.cooldown'
    try:
        current = float(path.read_text())
    except (FileNotFoundError, ValueError):
        current = 0.0
    path.write_text(str(max(current, time.time() + seconds)))


def model_free(model, kind=None):
    """True when the model is not resting and no worker holds its lock (checked by taking and releasing it)."""
    if cooling(model):
        return False
    held = claim(model, kind)
    if held is None:
        return False
    held.close()
    if is_zen_free(model):    # 2026-10-18: a Zen free model also needs the shared free channel
        channel = claim(FREE_CHANNEL)
        if channel is None:
            return False
        channel.close()
    return True


FREE_CHANNEL = 'free-channel'   # owner 2026-10-16: the Zen free models share one rate limit -> one at a time, rotating


def is_zen_free(model):
    return '/' not in model and model.endswith('-free')


def claim_free_channel():
    return claim(FREE_CHANNEL)


MULTI = ROOT / 'MULTI-SESSION.json'   # owner 2026-10-18: models that may run several sessions at once (read live)


def sessions(model):
    """Concurrent sessions allowed for a model: MULTI-SESSION.json {model: n}, default 1."""
    try:
        return max(1, int(load(MULTI, {}).get(model, 1)))
    except (TypeError, ValueError):
        return 1


def slot_range(model, kind=None):
    """The session slots a job of this kind ('feature' or 'bug') may take on a model. MULTI-SESSION.json
    "kind_sessions" {model: {"feature": n}} gives features the first n slots and bugs the rest (owner 2026-10-20:
    DeepSeek runs 2 bug and 2 feature sessions); without a split, or with no kind, every slot."""
    n = sessions(model)
    split = (load(MULTI, {}).get('kind_sessions') or {}).get(model)
    if not isinstance(split, dict) or kind not in ('feature', 'bug'):
        return range(n)
    f = min(n, max(0, int(split.get('feature', 0))))
    return range(f) if kind == 'feature' else range(f, n)


def claim(model, kind=None):
    """Non-blocking per-model lock (one of the model's slots for this kind of job), held by the returned file object
    until closed."""
    import fcntl
    LOCKS.mkdir(exist_ok=True)
    for i in slot_range(model, kind):
        fd = open(LOCKS / (f'{slug(model)}.lock' if i == 0 else f'{slug(model)}.{i + 1}.lock'), 'w')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            continue
        return fd
    return None


def workers():
    """Free-model sessions the fixer may run at once: the configured count, at most one per resting-free model."""
    if not ready():
        return 0
    cfg = load(CONFIG, {})
    usable = [m for m in cfg.get('models', []) if (m in ALLOWED or m in PAID_PLAN) and not cooling(m)]
    # the Zen free models count as ONE slot together (they rotate through the free channel)
    slots = sum(sessions(m) for m in usable if not is_zen_free(m)) + (1 if any(is_zen_free(m) for m in usable) else 0)
    return min(int(cfg.get('workers', 1)), slots, MAX_WORKERS)


def profile(model, cwd=None):
    full = full_id(model)
    provider, name = full.split('/', 1)
    config = {'$schema': 'https://opencode.ai/config.json', 'model': full, 'small_model': full,
            'enabled_providers': [provider], 'provider': {provider: {'whitelist': [name]}},
            'agent': {key: {'model': full} for key in ['build', 'title', 'summary', 'compaction']},
            'permission': {'*': 'allow', 'task': 'deny', 'webfetch': 'deny', 'websearch': 'deny',
                           'bash': {'*': 'allow', 'opencode*': 'deny', 'codex*': 'deny', 'mcode*': 'deny',
                                    'git push*': 'deny', 'git * push*': 'deny', 'gh *': 'deny'}},
            'share': 'disabled', 'autoupdate': False, 'lsp': False, 'formatter': False}
    # 2026-10-19: variants the provider API accepts but OpenCode's model list omits (Muse Spark's `max`; the API's
    # own error lists none/minimal/low/medium/high/xhigh/max), from OPENCODE-VARIANTS.json "_custom".
    try:
        custom = json.loads((ROOT / 'OPENCODE-VARIANTS.json').read_text()).get('_custom', {}).get(full)
    except (OSError, ValueError):
        custom = None
    if custom:
        config['provider'][provider]['models'] = {name: {'variants': custom}}
    if cwd is not None:
        config['mcp'] = {'fff': {'type': 'local', 'enabled': True, 'cwd': str(cwd),
            'command': [sys.executable, str(Path.home() / '.local/share/fff-proxy/fff_proxy.py')],
            'environment': {'FFF_PROXY_MAX_WORKERS': '3', 'FFF_MAX_CACHED_FILES': '2000'},
            'timeout': 30000}}
    return config


def tree_signature(cwd):
    head = subprocess.check_output(['git', '-C', str(cwd), 'rev-parse', 'HEAD'], text=True).strip()
    status = subprocess.check_output(['git', '-C', str(cwd), 'status', '--porcelain=v1'], text=True)
    return head, status


def text_from_events(events):
    return '\n\n'.join(e.get('part', {}).get('text', '') for e in events if e.get('type') == 'text')


def variant(model):
    """Owner 2026-10-19: "max for all cheap models". OPENCODE-VARIANTS.json (read live) names each model's highest
    reasoning variant from `opencode models --verbose`; a model with no variants list reasons at its own default."""
    try:
        table = json.loads((ROOT / 'OPENCODE-VARIANTS.json').read_text())
    except (OSError, ValueError):
        return None
    return table.get(full_id(model)) or table.get(model)


# ------------------------------------------------------------------ DeepSeek Harness
# Owner 2026-10-20: "run deepseek provided by opencode in the deepseek harness instead of the opencode harness".
# DSH-HARNESS.json (read live) lists the models that run through DeepSeek Harness (dsh headless, via run-dsh.sh:
# its profile routes OpenCode Go's OpenAI-compatible endpoint with OpenCode's own key, a stable x-opencode-session id,
# no telemetry, pushes blocked). Remove a model there (or the file) and it runs through OpenCode again.
DSH_RUN = Path.home() / '.local/share/zene-bunny-fix/dsh/run-dsh.sh'
DSH_HEADERS = Path.home() / '.local/share/zene-bunny-fix/dsh/home/zene-session-headers.json'
# OpenCode Go prices per million tokens (input, output, cached read), off-peak; DeepSeek doubles them in its peak
# hours (01:00-04:00 and 06:00-10:00 UTC, Monday-Friday). Used for the per-session cost cap and usage records.
GO_PRICES = {'opencode-go/deepseek-v4.1-flash': (0.15, 0.60, 0.003)}


def uses_dsh(model):
    try:
        return full_id(model) in json.loads((ROOT / 'DSH-HARNESS.json').read_text()).get('models', [])
    except (OSError, ValueError):
        return False


DSH_PROVIDERS = Path.home() / '.local/share/zene-bunny-fix/dsh/home/zene-session-providers.json'
PROVIDER_REST = Path.home() / '.local/share/zene-bunny-fix/dsh/home/provider-rest.json'
PROVIDER_LIMITS = Path.home() / '.local/share/zene-bunny-fix/dsh/home/provider-limits.json'   # plan-exhausted replies
PLAN_EXHAUSTED = re.compile(r'(?:coding_plan_exhausted|insufficient_balance).*?reset_at\W+([0-9T:.\-]+Z?)', re.S)


def ts_of(iso):
    try:
        return datetime.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def dsh_provider_weights():
    """Owner 2026-10-22 (A/B test): which providers serve DeepSeek in DeepSeek Harness, with weights -
    DSH-HARNESS.json "providers" {name: weight}; names match zene-patch.yml. Default: OpenCode Go only."""
    try:
        w = json.loads((ROOT / 'DSH-HARNESS.json').read_text()).get('providers') or {}
        return {k: float(v) for k, v in w.items() if float(v) > 0} or {'opencode-go': 1.0}
    except (OSError, ValueError, TypeError):
        return {'opencode-go': 1.0}


def dsh_providers_awake():
    rest = load(PROVIDER_REST, {})
    return [p for p in dsh_provider_weights() if float(rest.get(p, 0)) < time.time()]


def dsh_rest_provider(provider, seconds):
    """Rest one provider only, when another can take the work; returns False if it is the last one awake."""
    awake = [p for p in dsh_providers_awake() if p != provider]
    if not awake:
        return False
    rest = load(PROVIDER_REST, {})
    rest[provider] = max(float(rest.get(provider, 0)), time.time() + seconds)
    write(PROVIDER_REST, rest)
    print(f'DeepSeek Harness provider {provider} rests {int(seconds) // 60} min; {", ".join(awake)} takes new sessions.', flush=True)
    return True


def dsh_rest_provider_until(provider, until):
    """Rest a provider until a time (the usage monitor: OpenCode Go's plan limit), whether or not another is awake."""
    rest = load(PROVIDER_REST, {})
    rest[provider] = max(float(rest.get(provider, 0)), until)
    write(PROVIDER_REST, rest)


def dsh_pick_provider(session=None):
    """A resumed session stays on its provider; a new one gets a weighted random awake provider."""
    if session:
        known = load(DSH_PROVIDERS, {}).get(session)
        if known:
            return known
    weights = dsh_provider_weights()
    pool = dsh_providers_awake() or list(weights)
    return random.choices(pool, weights=[weights.get(p, 1.0) for p in pool])[0]


def deepseek_peak(now=None):
    t = datetime.datetime.fromtimestamp(now or time.time(), datetime.timezone.utc)
    return t.weekday() < 5 and (1 <= t.hour < 4 or 6 <= t.hour < 10)


def step_cost(model, usage):
    price = GO_PRICES.get(full_id(model))
    if not price:
        return 0.0
    cached = usage.get('cacheReadTokens') or 0
    fresh = max(0, (usage.get('inputTokens') or 0))
    cost = (fresh * price[0] + (usage.get('outputTokens') or 0) * price[1] + cached * price[2]) / 1e6
    return cost * (2 if deepseek_peak() else 1)


def run_model_dsh(model, cwd, brief, out, deadline=None, session=None):
    """run_model() for a model listed in DSH-HARNESS.json: same contract (returns (rc, transient)), same watchdog,
    cost cap, session file and usage file, with dsh headless --json events instead of OpenCode's."""
    env = dict(os.environ)
    headers = load(DSH_HEADERS, {})
    if session and headers.get(session):
        env['ZENE_OPENCODE_SESSION'] = headers[session]      # a resumed conversation keeps its routing/cache id
    else:
        env['ZENE_OPENCODE_SESSION'] = f'zene-{os.urandom(16).hex()}'
    provider = dsh_pick_provider(session)
    env['ZENE_DSH_PROVIDER'] = provider
    print(f'DeepSeek Harness provider: {provider}', flush=True)
    error_path = out.parent / f'opencode-{slug(model)}.stderr'
    events, steps, answer = [], [], ''
    cmd = [str(DSH_RUN), str(cwd), '--json'] + (['--session-id', session] if session else []) + ['-']
    with error_path.open('w') as error:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error, text=True, env=env,
                                start_new_session=True)
        def stop(signum, frame):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        def expire():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        timer = threading.Timer(max(1, deadline - time.time()), expire) if deadline else None
        if timer:
            timer.daemon = True
            timer.start()
        activity = {'last': time.time(), 'events': 0, 'stalled': None}
        def watchdog():
            while proc.poll() is None:
                quiet = time.time() - activity['last']
                limit = FIRST_EVENT_S if activity['events'] == 0 else QUIET_S
                if quiet > limit:
                    activity['stalled'] = 'no first event' if activity['events'] == 0 else f'quiet {int(quiet)} s'
                    expire()
                    return
                time.sleep(10)
        threading.Thread(target=watchdog, daemon=True).start()
        proc.stdin.write(brief)
        proc.stdin.close()
        for line in proc.stdout:
            activity['last'] = time.time()
            activity['events'] += 1
            sys.stdout.write(line)
            sys.stdout.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get('type')
            if kind == 'session' and event.get('sessionId') and not activity.get('session'):
                activity['session'] = event['sessionId']
                headers = load(DSH_HEADERS, {})
                headers[event['sessionId']] = env['ZENE_OPENCODE_SESSION']
                write(DSH_HEADERS, headers)
                providers = load(DSH_PROVIDERS, {})
                providers[event['sessionId']] = provider
                write(DSH_PROVIDERS, providers)
                write(out.parent / 'opencode-session.json', {'session': event['sessionId'], 'model': model,
                                                            'harness': 'dsh', 'provider': provider, 'continued': bool(session)})
            elif kind == 'status' and event.get('phase') == 'step_end':
                steps.append({**(event.get('usage') or {}), 'cost': step_cost(model, event.get('usage') or {})})
                u = event.get('usage') or {}
                with (ROOT / 'DSH-USAGE.jsonl').open('a') as ledger:   # plans.py: Go tokens that never reach opencode.db
                    ledger.write(json.dumps({'t': time.time(), 'model': full_id(model).split('/', 1)[1],
                                             'in': u.get('inputTokens') or 0, 'out': u.get('outputTokens') or 0,
                                             'cr': u.get('cacheReadTokens') or 0, 'cw': 0,
                                             'cost': steps[-1]['cost'], 'provider': provider}) + '\n')
            elif kind == 'final':
                answer = event.get('text') or answer
            elif kind == 'error':
                events.append(event)
            if model in PAID_PLAN and sum(st['cost'] for st in steps) > PAID_SESSION_CAP_USD:
                rest(model, 3600)
                write(out.parent / f'opencode-{slug(model)}-usage.json', {
                    'model': full_id(model), 'harness': 'dsh', 'provider': provider, 'stopped_for_nonzero_cost': True,
                    'step_costs': [st['cost'] for st in steps], 'steps': steps})
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                proc.wait()
                if timer:
                    timer.cancel()
                raise RuntimeError('DeepSeek Harness session passed the paid-session cap; this worker is stopped')
        rc = proc.wait()
        if timer:
            timer.cancel()
    if answer:
        out.write_text(answer + '\n')
    detail = '\n'.join(json.dumps(e) for e in events) + '\n' + error_path.read_text(errors='replace')
    failed = rc != 0 or bool(events) or not answer
    if activity['stalled']:
        print(f'DeepSeek Harness session for {model} went quiet ({activity["stalled"]}); treated as rate-limited.', flush=True)
        if not dsh_rest_provider(provider, LIMITED_REST):
            rest(model, LIMITED_REST)
    write(out.parent / f'opencode-{slug(model)}-usage.json', {
        'model': full_id(model), 'harness': 'dsh', 'provider': provider, 'exit': rc, 'stalled': activity['stalled'],
        'step_costs': [st['cost'] for st in steps], 'steps': steps, 'had_error': failed,
    })
    transient = bool(failed and (activity['stalled'] or TRANSIENT.search(detail)))
    exhausted = PLAN_EXHAUSTED.search(detail)
    if failed and exhausted:
        # 2026-10-24: OpenDesign answers 402 insufficient_balance with the plan's own reset time. Rest the provider
        # until then (not the 10-min COOLDOWN, which retried a dead plan ~20 times an hour for two days) and record it
        # for the status window; with no provider left awake the model rests until the first one wakes.
        until = ts_of(exhausted.group(1)) or time.time() + LIMITED_REST
        dsh_rest_provider_until(provider, until)
        limits = load(PROVIDER_LIMITS, {})
        limits[provider] = {'exhausted': True, 'reset_at': until, 'seen': time.time(), 'code': 'insufficient_balance'}
        write(PROVIDER_LIMITS, limits)
        if not dsh_providers_awake():
            rest(model, min(float(v) for v in load(PROVIDER_REST, {}).values()) - time.time())
        print(f'DeepSeek Harness provider {provider}: plan exhausted, rests until '
              f'{datetime.datetime.fromtimestamp(until):%a %d %b %H:%M}.', flush=True)
        (out.parent / 'dsh-provider-rested').write_text(provider)
    elif transient and not activity['stalled'] and dsh_rest_provider(provider, COOLDOWN):
        (out.parent / 'dsh-provider-rested').write_text(provider)   # run() leaves the model awake: the other provider takes over
    elif transient and activity['stalled'] and provider in load(PROVIDER_REST, {}):
        (out.parent / 'dsh-provider-rested').write_text(provider)
    return (1 if failed else 0), transient


MCODE_RUN = Path.home() / '.local/share/zene-bunny-fix/mcode/run-mcode.sh'
MCODE_HEADERS = Path.home() / '.local/share/zene-bunny-fix/mcode/data/zene-session-headers.json'


def uses_mcode(model):
    """Owner 2026-10-20 ("just move all of [MiniMax M3.1 Flash] to mcode"): models in MCODE-HARNESS.json (read live)
    run through MiniMax Code instead of OpenCode."""
    try:
        return full_id(model) in json.loads((ROOT / 'MCODE-HARNESS.json').read_text()).get('models', [])
    except (OSError, ValueError):
        return False


def run_model_mcode(model, cwd, brief, out, deadline=None, session=None):
    """run_model() for a model listed in MCODE-HARNESS.json: same contract (returns (rc, transient)), same watchdog,
    session file and usage file, with mcode exec --output-format stream-json events instead of OpenCode's."""
    env = dict(os.environ)
    headers = load(MCODE_HEADERS, {})
    if session and headers.get(session):
        env['ZENE_OPENCODE_SESSION'] = headers[session]      # a resumed conversation keeps its routing/cache id
    else:
        env['ZENE_OPENCODE_SESSION'] = f'zene-{os.urandom(16).hex()}'
    error_path = out.parent / f'opencode-{slug(model)}.stderr'
    events, turns, answer = [], [], ''
    cmd = [str(MCODE_RUN), str(cwd), full_id(model).split('/', 1)[1]] + (['--session', session] if session else [])
    with error_path.open('w') as error:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error, text=True, env=env,
                                start_new_session=True)
        def stop(signum, frame):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        def expire():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        timer = threading.Timer(max(1, deadline - time.time()), expire) if deadline else None
        if timer:
            timer.daemon = True
            timer.start()
        activity = {'last': time.time(), 'events': 0, 'stalled': None}
        def watchdog():
            while proc.poll() is None:
                quiet = time.time() - activity['last']
                limit = FIRST_EVENT_S if activity['events'] == 0 else QUIET_S
                if quiet > limit:
                    activity['stalled'] = 'no first event' if activity['events'] == 0 else f'quiet {int(quiet)} s'
                    expire()
                    return
                time.sleep(10)
        threading.Thread(target=watchdog, daemon=True).start()
        proc.stdin.write(brief)
        proc.stdin.close()
        for line in proc.stdout:
            activity['last'] = time.time()
            activity['events'] += 1
            sys.stdout.write(line)
            sys.stdout.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = str(event.get('type') or '')
            sid = event.get('sessionId')
            if sid and not activity.get('session'):
                activity['session'] = sid
                headers = load(MCODE_HEADERS, {})
                headers[sid] = env['ZENE_OPENCODE_SESSION']
                write(MCODE_HEADERS, headers)
                write(out.parent / 'opencode-session.json', {'session': sid, 'model': model,
                                                            'harness': 'mcode', 'continued': bool(session)})
            if kind == 'turn.completed':
                turns.append({**(event.get('usage') or {}), 'cost': 0.0})
            elif kind == 'exec.completed':
                result = event.get('result') or {}
                if result.get('status') == 'succeeded':
                    answer = result.get('output') or answer
                else:
                    events.append(event)
            elif kind.endswith(('.failed', '.error')) or kind == 'error':
                events.append(event)
        rc = proc.wait()
        if timer:
            timer.cancel()
    if answer:
        out.write_text(answer + '\n')
    detail = '\n'.join(json.dumps(e) for e in events) + '\n' + error_path.read_text(errors='replace')
    failed = rc != 0 or bool(events) or not answer
    if activity['stalled']:
        print(f'MiniMax Code session for {model} went quiet ({activity["stalled"]}); treated as rate-limited.', flush=True)
        rest(model, LIMITED_REST)
    write(out.parent / f'opencode-{slug(model)}-usage.json', {
        'model': full_id(model), 'harness': 'mcode', 'exit': rc, 'stalled': activity['stalled'],
        'step_costs': [t['cost'] for t in turns], 'steps': turns, 'had_error': failed,
    })
    return (1 if failed else 0), bool(failed and (activity['stalled'] or TRANSIENT.search(detail)))


def run_model(model, cwd, brief, out, deadline=None, session=None):
    # a session id from another harness cannot be resumed: that rework starts a fresh conversation
    # (ids: dsh 'session-...', mcode 'mvs_...', OpenCode 'ses_...')
    if uses_mcode(model):
        return run_model_mcode(model, cwd, brief, out, deadline, session if str(session).startswith('mvs_') else None)
    if uses_dsh(model):
        return run_model_dsh(model, cwd, brief, out, deadline, session if str(session).startswith('session-') else None)
    if str(session).startswith(('session-', 'mvs_')):
        session = None
    env = dict(os.environ)
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(profile(model, cwd))
    env['OPENCODE_DISABLE_PROJECT_CONFIG'] = 'true'
    env['OPENCODE_DISABLE_CLAUDE_CODE'] = 'true'
    error_path = out.parent / f'opencode-{slug(model)}.stderr'
    events = []
    with error_path.open('w') as error:
        proc = subprocess.Popen([CLI, 'run', '--pure', '--auto', '--model', full_id(model),
                                 '--format', 'json', '--title', 'Zene bug fix (free model)',
                                 '--dir', str(cwd)] + (['--session', session] if session else [])
                                + (['--variant', variant(model)] if variant(model) else []),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=error, text=True, env=env, start_new_session=True)
        def stop(signum, frame):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        # Used only when a caller passes a deadline (2026-10-21: the fix loop passes none - its sessions have no time cap;
        # the silence watchdog below and the driver's activity watchdog end a stalled one).
        def expire():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        timer = threading.Timer(max(1, deadline - time.time()), expire) if deadline else None
        if timer:
            timer.daemon = True
            timer.start()
        activity = {'last': time.time(), 'events': 0, 'stalled': None}
        def watchdog():
            while proc.poll() is None:
                quiet = time.time() - activity['last']
                limit = FIRST_EVENT_S if activity['events'] == 0 else QUIET_S
                if quiet > limit:
                    activity['stalled'] = 'no first event' if activity['events'] == 0 else f'quiet {int(quiet)} s'
                    expire()
                    return
                time.sleep(10)
        threading.Thread(target=watchdog, daemon=True).start()
        proc.stdin.write(brief)
        proc.stdin.close()
        for line in proc.stdout:
            activity['last'] = time.time()
            activity['events'] += 1
            sys.stdout.write(line)
            sys.stdout.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('sessionID') and not activity.get('session'):
                # owner 2026-10-18: a rework continues this session (same model, its context kept) to save tokens
                activity['session'] = event['sessionID']
                write(out.parent / 'opencode-session.json', {'session': event['sessionID'], 'model': model,
                                                            'continued': bool(session)})
            if event.get('type') in ('text', 'error', 'step_finish'):
                events.append(event)
            spent = sum(e.get('part', {}).get('cost') or 0 for e in events if e.get('type') == 'step_finish')
            over = spent > PAID_SESSION_CAP_USD if model in PAID_PLAN else \
                (event.get('type') == 'step_finish' and (event.get('part', {}).get('cost') or 0) > 0)
            if over:
                if model in PAID_PLAN:
                    rest(model, 3600)    # a session past the cap: this model rests, the free ones carry on
                else:
                    write(STATE, {'paused_until': time.time() + 3600,
                                  'last_error': 'OpenCode reported nonzero cost'})
                write(out.parent / f'opencode-{slug(model)}-usage.json', {
                    'model': full_id(model), 'stopped_for_nonzero_cost': True,
                    'step_costs': [e.get('part', {}).get('cost') for e in events if e.get('type') == 'step_finish']})
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                proc.wait()
                if timer:
                    timer.cancel()
                raise RuntimeError('OpenCode reported a nonzero cost; this worker is stopped')
        rc = proc.wait()
        if timer:
            timer.cancel()
    answer = text_from_events(events)
    if answer:
        out.write_text(answer + '\n')
    errors = [json.dumps(e) for e in events if e.get('type') == 'error']
    detail = '\n'.join(errors) + '\n' + error_path.read_text(errors='replace')
    failed = rc != 0 or bool(errors) or not answer
    if activity['stalled']:
        print(f'OpenCode session for {model} went quiet ({activity["stalled"]}); treated as rate-limited.', flush=True)
        rest(model, LIMITED_REST)
    write(out.parent / f'opencode-{slug(model)}-usage.json', {
        'model': full_id(model), 'exit': rc, 'stalled': activity['stalled'],
        'step_costs': [e.get('part', {}).get('cost') for e in events if e.get('type') == 'step_finish'],
        'had_error': failed,
    })
    return (1 if failed else 0), bool(failed and (activity['stalled'] or TRANSIENT.search(detail)))


PREFER = None
EXCLUDE = None   # models a job must not use (e.g. DeepSeek Free's 200k context on large features)
KIND = None      # 'feature' or 'bug': which of a model's session slots the job may take (slot_range)


def run(cwd, out):
    brief = sys.stdin.read()
    cfg = load(CONFIG, {})
    if not ready():
        return 75
    try:
        models = catalog()
    except Exception as error:
        write(STATE, {'paused_until': time.time() + 600, 'last_error': type(error).__name__})
        print('OpenCode free catalog could not be verified; retrying later.', flush=True)
        return 75
    allowed = [key for key in cfg['models'] if key in models or key in PAID_PLAN]
    state = load(STATE, {})
    if not allowed:
        write(STATE, {**state, 'paused_until': time.time() + 3600, 'last_error': 'No verified zero-cost models'})
        return 75
    index = state.get('next_index', 0) % len(allowed)
    ordered = allowed[index:] + allowed[:index]
    prefer = [m for m in (PREFER or '').split(',') if m in allowed]   # a feature tier's models go first
    ordered = prefer + [m for m in ordered if m not in prefer]
    ordered = [m for m in ordered if m not in (EXCLUDE or '').split(',')]
    baseline = tree_signature(cwd)
    deadline = None   # owner 2026-10-21: no wall-clock cap; the silence watchdog ends a stalled session
    resume = load(out.parent / 'resume.json', None)
    if resume and resume.get('session') and resume.get('model') in allowed and not cooling(resume['model']):
        # owner 2026-10-18: "fails -> goes back to the session that implemented it -> fails again -> moves up".
        # Only the implementing model may continue its session; if it is busy the job waits in the queue.
        model = resume['model']
        held = claim(model, KIND)
        if held is None:
            print('The implementing model is busy; the rework waits to continue its session.', flush=True)
            return 75
        channel = claim_free_channel() if is_zen_free(model) else None
        if is_zen_free(model) and channel is None:
            held.close()
            print('The free channel is busy; the rework waits to continue its session.', flush=True)
            return 75
        try:
            print(f"Continuing session {resume['session']} on {model}", flush=True)
            rc, transient = run_one(model, models, allowed, cwd, resume.get('brief') or brief, out, deadline,
                                    resume['session'])
            if rc != 0 and tree_signature(cwd) == baseline and not (out.parent / 'opencode-session.json').exists():
                # the session could not be continued (gone or refused): same model, fresh session, full brief
                print('Session could not be continued; starting a fresh session on the same model.', flush=True)
                rc, transient = run_one(model, models, allowed, cwd, brief, out, deadline)
            return rc
        finally:
            held.close()
            if channel is not None:
                channel.close()
    tried = 0
    for model in ordered:
        if (model not in PAID_PLAN and not eligible(models[model])) or cooling(model):
            continue
        held = claim(model, KIND)
        if held is None:          # another worker is on this model
            continue
        channel = None
        if is_zen_free(model):
            channel = claim_free_channel()
            if channel is None:   # another free model holds the shared free channel: rotate later
                held.close()
                continue
        tried += 1
        try:
            rc, transient = run_one(model, models, allowed, cwd, brief, out, deadline)
        finally:
            held.close()
            if channel is not None:
                channel.close()
        if rc == 0 or not transient or tree_signature(cwd) != baseline:
            return rc
        if not (out.parent / 'dsh-provider-rested').exists():
            rest(model)
        print('Free endpoint unavailable; unchanged worktree allows trying another verified free model.', flush=True)
    # No global pause: each failed model rests on its own (cooldown files), and workers() counts only rested ones.
    print('Every verified free model is busy or resting; the job stays queued.' if tried == 0 else
          'All free endpoints tried were unavailable; they rest and the job stays queued.', flush=True)
    return 75


def run_one(model, models, allowed, cwd, brief, out, deadline, session=None):
    info = models.get(model, {'name': model, 'cost': 'flat OpenCode Go plan (PAID_PLAN)'})
    write(out.parent / 'opencode-model.json', {'model': full_id(model),
          'name': info['name'], 'verified_cost': info['cost'], 'checked_at': time.time()})
    print('OpenCode model: ' + full_id(model), flush=True)
    rc, transient = run_model(model, cwd, brief, out, deadline, session)
    state = load(STATE, {})
    write(STATE, {**state, 'next_index': (allowed.index(model) + 1) % len(allowed),
                  'last_model': model, 'last_exit': rc})
    return rc, transient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'catalog', 'status'])
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--prefer', default=None, help='comma-separated models to try first')
    parser.add_argument('--exclude', default=None, help='comma-separated models this job must not use')
    parser.add_argument('--kind', default=None, choices=['feature', 'bug'], help="the job's kind, for split session slots")
    args = parser.parse_args()
    if args.action == 'run':
        if not args.cwd or not args.out:
            parser.error('run requires --cwd and --out')
        global PREFER, EXCLUDE, KIND
        PREFER = args.prefer
        EXCLUDE = args.exclude
        KIND = args.kind
        # 2026-10-22 (owner: "fix the ram issue"): /tmp is a RAM tmpfs and sessions filled it to 15/16 GB with scratch
        # trees; temp files go to disk instead, one dir per worktree
        tmp = Path.home() / '.cache/zene-tmp' / args.cwd.name
        tmp.mkdir(parents=True, exist_ok=True)
        os.environ['TMPDIR'] = str(tmp)
        sys.exit(run(args.cwd, args.out))
    if args.action == 'catalog':
        print(json.dumps({k: {'name': v['name'], 'cost': v['cost']} for k, v in catalog().items()}, indent=2))
    else:
        print(json.dumps({'ready': ready(), 'config': load(CONFIG, {}), 'state': load(STATE, {})}, indent=2))


if __name__ == '__main__':
    main()
