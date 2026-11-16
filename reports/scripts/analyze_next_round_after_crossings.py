"""Read-only immediate successor outcomes after BTC target crossings."""
import sys,json,math,hashlib
from pathlib import Path
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from collections import Counter
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import choppiness,price_history,round_history
now=datetime.now(ZoneInfo('America/Chicago'))
cutoff=datetime(2026,11,3,tzinfo=ZoneInfo('America/Chicago'))
raw=(ROOT/'round_history_btc.json').read_bytes()
rows=json.loads(raw)
by_start={datetime.fromisoformat(r['round_start']):r for r in rows}
pairs=[]
for start,row in sorted(by_start.items()):
    end=datetime.fromisoformat(row['round_end'])
    if start<cutoff or end>now:continue
    score=choppiness.score(price_history.load('BTC',start,end,include_volatility=False),start,end,row.get('target'),end,source='bitcoin')
    if score['status']!='ready':continue
    nxt=by_start.get(end)
    item=dict(trigger_start=start.isoformat(),trigger_crossings=score['score'],next_start=end.isoformat(),high=score['score']>6,trades=[])
    if end+timedelta(minutes=15)>now:item['outcome']='pending_round'
    elif nxt is None:item['outcome']='missing_round'
    else:
        for t in round_history.round_trades(nxt):
            if nxt.get('purchase_removed') and t is nxt.get('purchase'):continue
            if t.get('status')!='filled':continue
            pnl=round_history.trade_pnl(t,nxt.get('actual'))
            item['trades'].append(dict(pnl=pnl,direction=t.get('direction'),contracts=t.get('contracts')))
        values=[t['pnl'] for t in item['trades']]
        if not values:item['outcome']='no_buy'
        elif any(p is None for p in values):item['outcome']='unresolved'
        else:
            item['net']=round(math.fsum(values),4)
            item['outcome']='win' if item['net']>0 else 'loss' if item['net']<0 else 'flat'
    pairs.append(item)
def summary(items):
    counts=Counter(i['outcome'] for i in items)
    trades=[t for i in items for t in i['trades'] if t['pnl'] is not None]
    return dict(rounds=len(items),outcomes=dict(counts),loss_rate=counts['loss']/(counts['win']+counts['loss']) if counts['win']+counts['loss'] else None,net=round(math.fsum(i.get('net',0) for i in items),4),trades=len(trades),winning_trades=sum(t['pnl']>0 for t in trades),losing_trades=sum(t['pnl']<0 for t in trades))
high=[i for i in pairs if i['high']]
report=dict(cutoff=cutoff.isoformat(),asof=now.isoformat(),methodology='Production BTC target strict crossing count with causal 15-second sampling; gaps skipped. Trigger >6 means >=7. Immediate scheduled next round, including no buys. Actual filled trade net P/L summed by successor round. No settings or trading changes.',journal_sha256=hashlib.sha256(raw).hexdigest(),high=summary(high),comparison_at_most_6=summary([i for i in pairs if not i['high']]),daily={d:summary([i for i in high if i['next_start'][:10]==d]) for d in sorted({i['next_start'][:10] for i in high})},high_pairs=high)
path=ROOT/'reports/next_round_after_7_crossings_since_20261103.json'
path.write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k!='high_pairs'},indent=2))
