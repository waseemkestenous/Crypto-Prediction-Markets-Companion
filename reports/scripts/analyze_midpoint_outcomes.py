"""Read-only analysis using the production midpoint-crossing score, no trading changes."""
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import choppiness
import order_settings
import price_history
import round_history

NOW = datetime.now(ZoneInfo('America/Chicago'))
CUTOFF = datetime(2026,11,3,tzinfo=ZoneInfo('America/Chicago'))
raw = (ROOT/'round_history_btc.json').read_bytes()
rows = json.loads(raw)
by_start = {datetime.fromisoformat(row['round_start']):row for row in rows}
config = order_settings.load()['choppiness_buy_guard']
lookback = config['rounds']
threshold = config['max_score']
points_cache = {}
score_cache = {}
file_hashes = {}
excluded = Counter()
trades = []

def points(row, source):
    start,end = [datetime.fromisoformat(row[k]) for k in ('round_start','round_end')]
    key = (start,source)
    if key not in points_cache:
        if source=='contract_yes':
            points_cache[key] = row.get('contract_price_history',[])
        else:
            path = price_history.path_for('BTC',start)
            if path.exists():
                file_hashes[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
            points_cache[key] = price_history.load('BTC',start,end,include_volatility=False)
    return points_cache[key]

def scored(row,source,asof=None):
    start,end = [datetime.fromisoformat(row[k]) for k in ('round_start','round_end')]
    stamp = asof or end
    key = (start,source,stamp)
    if key not in score_cache:
        score_cache[key] = choppiness.score(points(row,source),start,end,row.get('target'),stamp,source=source)
    return score_cache[key]

def prior_scores(start,source):
    result=[]
    for lag in range(1,lookback+1):
        prev=by_start.get(start-timedelta(minutes=15*lag))
        result.append(scored(prev,source) if prev else None)
    return result

for row in rows:
    start,end=[datetime.fromisoformat(row[k]) for k in ('round_start','round_end')]
    if not CUTOFF<=start<=NOW:
        continue
    for index,trade in enumerate(round_history.round_trades(row)):
        if row.get('purchase_removed') and trade is row.get('purchase'):
            excluded['removed']+=1
            continue
        if trade.get('status')!='filled':
            excluded[trade.get('status','unknown')]+=1
            continue
        pnl=round_history.trade_pnl(trade,row.get('actual'))
        if pnl is None:
            excluded['unresolved']+=1
            continue
        if end>NOW:
            excluded['round_still_running']+=1
            continue
        try:
            decision=datetime.fromisoformat(trade.get('attempted_at') or trade['submitted_at'])
        except (KeyError,ValueError,TypeError):
            excluded['missing_entry_time']+=1
            continue
        item=dict(round_start=row['round_start'],round_end=row['round_end'],trade_index=index,
            decision_at=decision.isoformat(),entry_minute=(decision-start).total_seconds()/60,
            outcome='win' if pnl>0 else 'loss' if pnl<0 else 'flat',pnl=pnl,contracts=trade['contracts'],
            side='Yes' if trade['direction']=='UP / ABOVE' else 'No',
            entry_cents=trade.get('execution_price_cents',trade.get('limit_price_cents')),
            rule=f"FB{trade.get('fallback_rule_number')}" if trade.get('purchase_rule')=='fallback' else trade.get('purchase_rule'),
            closed_early=(trade.get('close_order') or {}).get('status')=='filled')
        for source in ('bitcoin','contract_yes'):
            prev=prior_scores(start,source)
            valid_prev=[p for p in prev if p and p['status']=='ready']
            item[source]=dict(full=scored(row,source),before_entry=scored(row,source,min(decision,end)),
                previous_rounds=prev,previous_average=statistics.mean(p['score'] for p in valid_prev) if len(valid_prev)==lookback else None)
        trades.append(item)

def basic(items):
    losses=sum(t['outcome']=='loss' for t in items)
    wins=sum(t['outcome']=='win' for t in items)
    return dict(trades=len(items),wins=wins,losses=losses,flats=len(items)-wins-losses,
        loss_rate=losses/len(items) if items else None,net=round(math.fsum(t['pnl'] for t in items),4),
        mean_entry_minute=round(statistics.mean(t['entry_minute'] for t in items),3) if items else None)

def stats(items,source):
    result=basic(items)
    for label,key in [('full','full'),('before_entry','before_entry')]:
        usable=[t[source][key] for t in items if t[source][key]['status']=='ready']
        result[label]=dict(measured=len(usable),mean=statistics.mean(p['score'] for p in usable) if usable else None,
            median=statistics.median(p['score'] for p in usable) if usable else None,
            total= sum(p['score'] for p in usable),zero=sum(p['score']==0 for p in usable),
            up=sum(p['crossings_up'] for p in usable),down=sum(p['crossings_down'] for p in usable),
            partial=sum(p['missing_samples']>0 for p in usable))
    averages=[t[source]['previous_average'] for t in items if t[source]['previous_average'] is not None]
    result['previous_average']=dict(measured=len(averages),mean=statistics.mean(averages) if averages else None,
                                   median=statistics.median(averages) if averages else None)
    return result

def count_bucket(n):
    return '0' if n==0 else '1-2' if n<=2 else '3-5' if n<=5 else '6+'

def previous_bucket(n):
    return '0-1' if n<=1 else '>1-3' if n<=3 else '>3-5' if n<=5 else '>5'

def fisher(a,b,c,d):
    n=a+b+c+d;r=a+b;s=a+c
    if not n:return None
    def probability(k):return math.comb(s,k)*math.comb(n-s,r-k)/math.comb(n,r)
    observed=probability(a)
    return min(1,sum(probability(k) for k in range(max(0,r-(n-s)),min(r,s)+1) if probability(k)<=observed+1e-12))

results={}
for source in ('bitcoin','contract_yes'):
    groups={}
    for label,key,bucket,levels in [('full_round','full',count_bucket,['0','1-2','3-5','6+']),
                                  ('before_entry','before_entry',count_bucket,['0','1-2','3-5','6+'])]:
        groups[label]={v:basic([t for t in trades if t[source][key]['status']=='ready' and bucket(t[source][key]['score'])==v]) for v in levels}
    groups['previous_average']={v:basic([t for t in trades if t[source]['previous_average'] is not None and previous_bucket(t[source]['previous_average'])==v]) for v in ('0-1','>1-3','>3-5','>5')}
    yes=[t for t in trades if t[source]['previous_average'] is not None and t[source]['previous_average']>threshold]
    no=[t for t in trades if t[source]['previous_average'] is not None and t[source]['previous_average']<=threshold]
    high,low=basic(yes),basic(no)
    results[source]=dict(outcomes={o:stats([t for t in trades if t['outcome']==o],source) for o in ('win','loss','flat')},
        groups=groups,threshold=dict(value=threshold,above=high,at_or_below=low,missing=len(trades)-len(yes)-len(no),
            fisher_p=fisher(high['losses'],high['wins'],low['losses'],low['wins']),
            dates={d:dict(above=basic([t for t in yes if t['round_start'].startswith(d)]),
                          at_or_below=basic([t for t in no if t['round_start'].startswith(d)])) for d in sorted(set(t['round_start'][:10] for t in trades))}),
        daily={d:stats([t for t in trades if t['round_start'].startswith(d)],source) for d in sorted(set(t['round_start'][:10] for t in trades))})

# Sensitivity: avoid giving an extra weight to a round with multiple trades.
unique={}
for t in sorted(trades,key=lambda t:t['decision_at']):unique.setdefault(t['round_start'],t)
sensitivity={}
for source in ('bitcoin','contract_yes'):
    sensitivity[source]={str(flag):basic([t for t in unique.values() if t[source]['previous_average'] is not None and (t[source]['previous_average']>threshold)==flag]) for flag in (True,False)}

report=dict(cutoff=CUTOFF.isoformat(),asof=NOW.isoformat(),population=basic(trades),unique_rounds=len(unique),excluded=dict(excluded),
    metric=choppiness.METRIC,lookback_rounds=lookback,threshold=threshold,
    methodology='Same production score: causal 15-second samples, most recent price <=30 seconds old; gaps skipped, minimum 4 usable samples; strict crossings of BTC round target / Yes 50 cents, exact touches and initial side ignored. Full-round counts are descriptive, not available at entry. Before-entry counts use attempted_at (last pre-Buy timestamp) and no later points. Prior average uses exactly N consecutive completed rounds including unbought ones; no replacement for missing rounds. One filled trade counts once regardless of CT quantity. Net P/L uses journal.trade_pnl (recorded fees/prices where available, default fee 1.94 cents per CT otherwise). No app/journal/settings changes.',
    journal_sha256=hashlib.sha256(raw).hexdigest(),score_code_sha256=hashlib.sha256((ROOT/'choppiness.py').read_bytes()).hexdigest(),
    price_file_sha256=file_hashes,results=results,unique_round_sensitivity=sensitivity,trades=trades)
output=ROOT/'reports/midpoint_crossing_outcomes_since_20261103.json'
output.write_text(json.dumps(report,indent=2))
print(json.dumps({k:report[k] for k in ('cutoff','asof','population','unique_rounds','excluded','lookback_rounds','threshold','results','unique_round_sensitivity')},indent=2))
