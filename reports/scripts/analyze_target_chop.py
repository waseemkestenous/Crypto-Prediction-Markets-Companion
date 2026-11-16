"""Exploratory retrospective analysis. Reads prices/journal, writes only report."""
import json, math, statistics, sys, hashlib
from datetime import datetime, timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import price_history

baseline=json.loads((ROOT/'reports/contract_50_crossings_since_20261103.json').read_text())
raw=(ROOT/'round_history_btc.json').read_bytes()
rows=json.loads(raw)
by_start={r['round_start']:r for r in rows}
source_files={}

def points(row):
    start,end=[datetime.fromisoformat(row[k]) for k in ('round_start','round_end')]
    path=price_history.path_for('BTC',start)
    if path.exists():source_files[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return [(datetime.fromisoformat(p['observed_at']),p['price']) for p in price_history.load('BTC',start,end)]

cache={}
def history(row):
    if row['round_start'] not in cache:cache[row['round_start']]=points(row)
    return cache[row['round_start']]

def metrics(samples,target,start,end):
    samples=[(t,p) for t,p in samples if start<=t<=end]
    duration=(end-start).total_seconds()
    if len(samples)<2 or not isinstance(target,(int,float)) or not math.isfinite(target):return None
    coverage=(samples[-1][0]-samples[0][0]).total_seconds()
    gaps=[(b[0]-a[0]).total_seconds() for a,b in zip(samples,samples[1:])]
    if coverage<duration*.8 or max(gaps,default=0)>20 or (samples[0][0]-start).total_seconds()>20 or (end-samples[-1][0]).total_seconds()>20:return None
    crossings=wide=0
    last=last_wide=0
    near=0
    movement=0
    for i,(stamp,p) in enumerate(samples):
        side=(p>target)-(p<target)
        if side:
            crossings+=bool(last and last!=side)
            last=side
        side_wide=1 if p>target+10 else -1 if p<target-10 else 0
        if side_wide:
            wide+=bool(last_wide and last_wide!=side_wide)
            last_wide=side_wide
        if i+1<len(samples):
            elapsed=(samples[i+1][0]-stamp).total_seconds()
            near+=elapsed*(abs(p-target)<=25)
            movement+=abs(samples[i+1][1]-p)
    return dict(crossings=crossings, wide_crossings_10d=wide, near25_share=near/coverage,
        efficiency=abs(samples[-1][1]-samples[0][1])/movement if movement else 0,
        coverage_seconds=coverage,samples=len(samples))

round_metrics={}
for row in rows:
    if '2026-11-02'<=row['round_start']<=baseline['asof']:
        start,end=[datetime.fromisoformat(row[k]) for k in ('round_start','round_end')]
        if end<=datetime.fromisoformat(baseline['asof']):round_metrics[row['round_start']]=metrics(history(row),row.get('target'),start,end)

trades=[]
for original in baseline['trades']:
    t=dict(original)
    row=by_start[t['round_start']]
    start=datetime.fromisoformat(t['round_start']);entry=datetime.fromisoformat(t['entry'])
    samples=history(row)
    for seconds in (60,120):t[f'pre{seconds}']=metrics(samples,row.get('target'),entry-timedelta(seconds=seconds),entry)
    previous=[]
    for lag in (1,2,3):
        prev=by_start.get((start-timedelta(minutes=15*lag)).isoformat())
        previous.append(round_metrics.get(prev['round_start']) if prev else None)
    t['previous_round']=previous[0]
    t['previous3_crossings_mean']=statistics.mean(p['crossings'] for p in previous) if all(previous) else None
    t['previous3_wide_crossings_mean']=statistics.mean(p['wide_crossings_10d'] for p in previous) if all(previous) else None
    t['previous3_near25_mean']=statistics.mean(p['near25_share'] for p in previous) if all(previous) else None
    t['current_round']=round_metrics.get(t['round_start'])
    trades.append(t)

def fisher(a,b,c,d):
    n=a+b+c+d;r=a+b;s=a+c
    def prob(k):return math.comb(s,k)*math.comb(n-s,r-k)/math.comb(n,r)
    p=prob(a)
    return min(1,sum(prob(k) for k in range(max(0,r-(n-s)),min(r,s)+1) if prob(k)<=p+1e-12))

def summary(ts):
    n=len(ts);loss=sum(t['outcome']=='loss' for t in ts)
    return dict(n=n,wins=n-loss,losses=loss,loss_rate=loss/n if n else None,net=round(sum(t['pnl'] for t in ts),4))

rules={
 'pre60_cross2':lambda t:None if t['pre60'] is None else t['pre60']['crossings']>=2,
 'pre60_near25_half':lambda t:None if t['pre60'] is None else t['pre60']['near25_share']>=.5,
 'pre60_cross2_and_near25_half':lambda t:None if t['pre60'] is None else t['pre60']['crossings']>=2 and t['pre60']['near25_share']>=.5,
 'pre120_cross2':lambda t:None if t['pre120'] is None else t['pre120']['crossings']>=2,
 'pre120_near25_half':lambda t:None if t['pre120'] is None else t['pre120']['near25_share']>=.5,
 'pre120_cross2_and_near25_half':lambda t:None if t['pre120'] is None else t['pre120']['crossings']>=2 and t['pre120']['near25_share']>=.5,
 'previous_round_cross4':lambda t:None if t['previous_round'] is None else t['previous_round']['crossings']>=4,
 'previous3_mean_cross4':lambda t:None if t['previous3_crossings_mean'] is None else t['previous3_crossings_mean']>=4,
 'previous3_mean_wide_cross2':lambda t:None if t['previous3_wide_crossings_mean'] is None else t['previous3_wide_crossings_mean']>=2,
 'previous3_near25_half':lambda t:None if t['previous3_near25_mean'] is None else t['previous3_near25_mean']>=.5,
 'current_round_cross6_DESCRIPTIVE':lambda t:None if t['current_round'] is None else t['current_round']['crossings']>=6,
}
results={}
for name,fn in rules.items():
    yes=[t for t in trades if fn(t) is True];no=[t for t in trades if fn(t) is False]
    a,b=summary(yes),summary(no)
    results[name]=dict(flagged=a,unflagged=b,missing=len(trades)-len(yes)-len(no),fisher_p=fisher(a['losses'],a['wins'],b['losses'],b['wins']) if yes and no else None,
      dates={date:dict(flagged=summary([t for t in yes if t['round_start'].startswith(date)]),unflagged=summary([t for t in no if t['round_start'].startswith(date)])) for date in ('2026-11-03','2026-11-04','2026-11-05')})
blocks={}
for t in trades:
    start=datetime.fromisoformat(t['round_start']);key=start.replace(hour=start.hour//2*2,minute=0,second=0).isoformat()
    blocks.setdefault(key,[]).append(t)
blocks={k:dict(summary(v),mean_round_crossings=statistics.mean(t['current_round']['crossings'] for t in v if t['current_round']) if any(t['current_round'] for t in v) else None,
             previous3_cross4_n=sum(rules['previous3_mean_cross4'](t) is True for t in v)) for k,v in blocks.items()}
report=dict(asof=baseline['asof'],cutoff=baseline['cutoff'],population='Same 108 completed trades as preceding crossing report; exclude open/cancelled orders.',
    journal_sha256=hashlib.sha256(raw).hexdigest(),price_file_sha256=source_files,
    definitions='BTC quote crossings of each round target; exact touches ignored. Near means within $25, time weighted. Wide crossings use target +/-$10. Pre-entry windows require >=80% coverage and max gaps <=20s. Previous rounds are strictly completed before entry, include rounds without trades. Full-current-round measure is descriptive only, unavailable at entry. Exploratory thresholds, no held-out validation; all P/L uses existing modeled journal fees.',
    summary=summary(trades),rules=results,blocks_2h=blocks,trades=trades)
(ROOT/'reports/target_chop_since_20261103.json').write_text(json.dumps(report,indent=2))
print(json.dumps({k:report[k] for k in ('summary','rules','blocks_2h')},indent=2))
