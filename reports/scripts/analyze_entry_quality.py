"""Prospective snapshot audit. Read-only sources; exploratory rules, no trading changes."""
import json, math, sys, hashlib, argparse
from pathlib import Path
from datetime import datetime
from collections import Counter
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import round_history
parser=argparse.ArgumentParser()
parser.add_argument('--through', help='Last round start to include (ISO timestamp)')
parser.add_argument('--output', default='reports/entry_quality_20261106_20261109.json')
args=parser.parse_args()
DIRECTIONS=('UP / ABOVE','DOWN / BELOW')
def stamp(s): return datetime.fromisoformat(s).timestamp()
def finite(v): return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
def summarize(items):
    n=len(items); wins=sum(i['hit'] for i in items)
    if not n:return {'n':0}
    p=wins/n; z=1.96; den=1+z*z/n
    middle=(p+z*z/(2*n))/den; half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return {'n':n,'wins':wins,'accuracy_pct':round(p*100,2),'wilson95_pct':[round(100*(middle-half),2),round(100*(middle+half),2)],'direction_counts':dict(Counter(i['direction'] for i in items)),'mean_entry_cents':round(sum(i['price'] for i in items)/n,2),'hypothetical_settlement_net_cents_per_contract':round(sum((100 if i['hit'] else 0)-i['price']-1.94 for i in items)/n,3)}
raw=(ROOT/'round_history_btc.json').read_bytes(); journal=json.loads(raw)
by_start={stamp(r['round_start']):r for r in journal}
files=sorted((ROOT/'research_data/entry_quality_v1/btc').glob('*.jsonl'))
if args.through:
    files=[p for p in files if int(p.stem)<=stamp(args.through)]
rounds=[]; excluded=Counter(); hashes={}; snapshots=0; invalid=0; actual_trades=[]
for file in files:
    content=file.read_bytes();hashes[file.name]=hashlib.sha256(content).hexdigest(); records=[]
    for line in content.splitlines():
        try:
            r=json.loads(line)
            if r['round_start']<'2026-11-06':continue
            records.append(r)
        except (ValueError,KeyError):invalid+=1
    if not records:continue
    records.sort(key=lambda r:stamp(r['observed_at']));snapshots+=len(records)
    row=by_start.get(stamp(records[0]['round_start']))
    if not row or row.get('actual') not in DIRECTIONS or not finite(row.get('settlement')) or not finite(row.get('target')) or (row['settlement']>=row['target'])!=(row['actual']==DIRECTIONS[0]):
        excluded['missing_or_inconsistent_settlement']+=1;continue
    qualified=[]
    for r in records:
        t=stamp(r['observed_at']); start=stamp(r['round_start']); minute=(t-start)/60
        rec=r.get('live_recommendation') or {};q=r.get('contract_quotes') or {}; d=r.get('target_delta_dollars');c=r.get('crossings_120s') or {}
        if not (3<=minute<=12 and finite(d) and d!=0):continue
        direction=DIRECTIONS[0] if d>0 else DIRECTIONS[1]
        if rec.get('recommendation')!=direction:continue
        try: age=t-stamp(q['updated_at']); rec_age=t-stamp(rec['updated_at'])
        except (KeyError,TypeError,ValueError):continue
        if not (finite(r.get('btc_quote_age_seconds')) and 0<=r['btc_quote_age_seconds']<=10 and 0<=age<=10 and 0<=rec_age<=35):continue
        if q.get('target')!=r['target'] or stamp(q.get('round_start',r['round_start']))!=start:continue
        if direction==DIRECTIONS[1] and q.get('no_is_estimate'):continue
        price=q.get('yes_cents' if direction==DIRECTIONS[0] else 'no_cents')
        if not finite(price) or not 40<=price<=80:continue
        norm=r.get('normalized_target_distance');stable=r.get('observed_same_side_seconds');model=r.get('model_snapshot') or {}
        probabilities=[model.get(k) for k in ('p_pattern','p_regime','p_bootstrap','p_trend')]
        agreement=sum((p>=.5)==(direction==DIRECTIONS[0]) for p in probabilities if finite(p)) if all(finite(p) for p in probabilities) else None
        qualified.append(dict(time=r['observed_at'],minute=minute,direction=direction,price=price,hit=direction==row['actual'],distance=abs(d),norm=abs(norm) if finite(norm) else None,stable=stable,crossings=c.get('score') if c.get('status')=='ready' and c.get('missing_samples')==0 else None,agreement=agreement))
    rounds.append(dict(start=row['round_start'],candidates=qualified))
    for trade in round_history.round_trades(row):
        if trade.get('status')!='filled' or (row.get('purchase_removed') and trade is row.get('purchase')):continue
        pnl=round_history.trade_pnl(trade,row['actual'])
        if pnl is None:continue
        try: decision=stamp(trade.get('submitted_at') or trade['attempted_at'])
        except (KeyError,ValueError):continue
        prior=[r for r in records if stamp(r['observed_at'])<=decision and decision-stamp(r['observed_at'])<=35]
        snap=prior[-1] if prior else {}
        actual_trades.append(dict(round=row['round_start'],test=bool(trade.get('test_mode')),pnl=pnl,win=pnl>0,snapshot_available=bool(snap),distance=abs(snap['target_delta_dollars']) if finite(snap.get('target_delta_dollars')) else None,stable=snap.get('observed_same_side_seconds'),norm=abs(snap['normalized_target_distance']) if finite(snap.get('normalized_target_distance')) else None))
rounds.sort(key=lambda r:stamp(r['start']));cut=int(len(rounds)*.7)
rules={
 'baseline':lambda x:True,
 'distance_ge_25':lambda x:x['distance']>=25,
 'distance_ge_50':lambda x:x['distance']>=50,
 'normalized_ge_0.5':lambda x:x['norm'] is not None and x['norm']>=.5,
 'normalized_ge_1':lambda x:x['norm'] is not None and x['norm']>=1,
 'stable_ge_30':lambda x:finite(x['stable']) and x['stable']>=30,
 'stable_ge_60':lambda x:finite(x['stable']) and x['stable']>=60,
 'stable_ge_120':lambda x:finite(x['stable']) and x['stable']>=120,
 'crossings_le_1':lambda x:finite(x['crossings']) and x['crossings']<=1,
 'models_ge_3':lambda x:x['agreement'] is not None and x['agreement']>=3,
 'distance25_stable30':lambda x:x['distance']>=25 and finite(x['stable']) and x['stable']>=30,
 'distance25_stable60':lambda x:x['distance']>=25 and finite(x['stable']) and x['stable']>=60,
 'norm05_stable60':lambda x:x['norm'] is not None and x['norm']>=.5 and finite(x['stable']) and x['stable']>=60,
}
for duration in (15,30,45,60,90,120,180):
    rules[f'stable_ge_{duration}']=lambda x,duration=duration:finite(x['stable']) and x['stable']>=duration
    rules[f'distance25_stable{duration}']=lambda x,duration=duration:x['distance']>=25 and finite(x['stable']) and x['stable']>=duration
results={}
for name,rule in rules.items():
    groups={}
    for key,subset in [('all',rounds),('earlier',rounds[:cut]),('later',rounds[cut:])]:
        chosen=[next((c for c in r['candidates'] if rule(c)),None) for r in subset]
        groups[key]=summarize([c for c in chosen if c])
    results[name]=groups
entry_time_groups={}
for name in ('stable_ge_90','distance25_stable90'):
    selected=[next((c for c in r['candidates'] if rules[name](c)),None) for r in rounds]
    entry_time_groups[name]={f'{lo}–{hi}':summarize([c for c in selected if c and lo<=c['minute']<hi])
                             for lo,hi in ((0,5),(5,10),(10,15))}
trade_stats={}
for mode in (False,True):
    trades=[t for t in actual_trades if t['test']==mode]
    trade_stats['virtual' if mode else 'recorded_real']={'n':len(trades),'wins':sum(t['win'] for t in trades),'net_pnl_dollars':round(sum(t['pnl'] for t in trades),4),'with_preceding_snapshot':sum(t['snapshot_available'] for t in trades),'distinct_rounds':len(set(t['round'] for t in trades))}
trade_groups={}
for field,edges in [('distance',[0,25,50,100,float('inf')]),('stable',[0,30,60,120,float('inf')]),('norm',[0,.5,1,2,float('inf')])]:
    trade_groups[field]=[]
    for lo,hi in zip(edges,edges[1:]):
        ts=[t for t in actual_trades if not t['test'] and finite(t[field]) and lo<=t[field]<hi]
        trade_groups[field].append({'bin':f'{lo}–{hi}','n':len(ts),'wins':sum(t['win'] for t in ts),'net_pnl_dollars':round(sum(t['pnl'] for t in ts),4)})
report=dict(source_journal_sha256=hashlib.sha256(raw).hexdigest(),research_file_hashes=hashes,research_round_files=len(files),snapshots=snapshots,invalid_lines=invalid,settled_rounds=len(rounds),excluded=dict(excluded),first_round=rounds[0]['start'],last_round=rounds[-1]['start'],later_period_start=rounds[cut]['start'],method='One first qualifying observation per round per rule; minutes 3–12; live agrees with BTC side; contract 40–80c; BTC and contract age <=10s; estimated No excluded. Chronological 70/30 split is exploratory, not untouched external validation. Net settlement illustration subtracts assumed 1.94 cents buy fee per contract; ignores execution/slippage and early exits. Persistence is observed, not guaranteed continuous.',rules=results,entry_time_groups=entry_time_groups,actual_trades=trade_stats,actual_trade_groups=trade_groups)
out=ROOT/args.output;out.write_text(json.dumps(report,indent=2,allow_nan=False))
print(json.dumps({k:v for k,v in report.items() if k not in ('research_file_hashes','source_journal_sha256')},indent=2))
