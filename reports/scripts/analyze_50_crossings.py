"""Read-only historical trade/quote analysis; no settings or journal changes."""
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import round_history

now = datetime.now(ZoneInfo('America/Chicago'))
cutoff = datetime(2026, 11, 3, tzinfo=ZoneInfo('America/Chicago'))
raw = (ROOT/'round_history_btc.json').read_bytes()
records = json.loads(raw)
excluded = Counter()
trades = []

def crosses(samples, low=50, high=50):
    previous = None
    events = []
    for t, price in samples:
        side = 1 if price > high else -1 if price < low else 0
        if side == 0:
            continue
        if previous is not None and side != previous:
            events.append({'at': t.isoformat(), 'direction': 'up' if side == 1 else 'down', 'price_cents': price})
        previous = side
    return events

for row in records:
    start, end = (datetime.fromisoformat(row[k]) for k in ('round_start', 'round_end'))
    if not cutoff <= start <= now:
        continue
    for index, trade in enumerate(round_history.round_trades(row)):
        pnl = round_history.trade_pnl(trade, row.get('actual'))
        if pnl is None:
            excluded[trade.get('status', 'unknown')] += 1
            continue
        if row.get('purchase_removed') and trade is row.get('purchase'):
            excluded['removed'] += 1
            continue
        entry = datetime.fromisoformat(trade.get('submitted_at') or trade['attempted_at'])
        closing = trade.get('close_order') or {}
        exit_time = datetime.fromisoformat(closing.get('filled_at') or closing.get('submitted_at') or closing['attempted_at']) if closing.get('status') == 'filled' else end
        key = 'yes_cents' if trade['direction'] == 'UP / ABOVE' else 'no_cents'
        samples = {}
        for quote in row.get('contract_price_history', []):
            try:
                t = datetime.fromisoformat(quote['observed_at'])
                price = quote[key]
                if start <= t < min(end, now) and quote.get('target') == row.get('target') and isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) and 0 <= price <= 100:
                    samples[t] = price
            except (ValueError, TypeError, KeyError):
                pass
        samples = sorted(samples.items())
        held = [(t,p) for t,p in samples if entry <= t <= exit_time]
        before = [(t,p) for t,p in samples if t < entry]
        events = crosses(held)
        full_events = crosses(samples)
        last_minute = [(t,p) for t,p in samples if (entry-t).total_seconds() >= 0 and (entry-t).total_seconds() <= 60]
        conditions = trade.get('conditions') or {}
        fallback = conditions.get('fallback') or {}
        stop = fallback.get('stop_loss') or conditions.get('stop_loss') or {}
        confidence = (trade.get('live_recommendation') or {}).get('confidence')
        last = events[-1] if events else None
        trades.append(dict(round_start=row['round_start'], trade_index=index, entry=entry.isoformat(), exit=exit_time.isoformat(),
            outcome='win' if pnl > 0 else 'loss' if pnl < 0 else 'flat', pnl=pnl, contracts=trade['contracts'],
            side='Yes' if key=='yes_cents' else 'No', entry_cents=trade.get('execution_price_cents',trade.get('limit_price_cents')),
            entry_minute=(entry-start).total_seconds()/60, held_minutes=(exit_time-entry).total_seconds()/60,
            rule=f"FB{trade.get('fallback_rule_number')}" if trade.get('purchase_rule')=='fallback' else trade.get('purchase_rule'),
            stop_enabled=stop.get('enabled'), final_agrees=row.get('prediction')==trade['direction'], confidence=confidence,
            quote_samples=len(held), first_quote=held[0][0].isoformat() if held else None, last_quote=held[-1][0].isoformat() if held else None,
            max_quote_gap_seconds=max(((b[0]-a[0]).total_seconds() for a,b in zip(held,held[1:])),default=None),
            crosses_held=len(events), up_held=sum(e['direction']=='up' for e in events), down_held=sum(e['direction']=='down' for e in events),
            crosses_round=len(full_events), crosses_before_entry=len(crosses(before)), crosses_last60_before_entry=len(crosses(last_minute)),
            wide_crosses_held=len(crosses(held,45,55)), last_cross_direction=last['direction'] if last else None,
            last_cross_minute=(datetime.fromisoformat(last['at'])-start).total_seconds()/60 if last else None,
            ever_below50_held=any(p<50 for _,p in held), max_held_cents=max((p for _,p in held),default=None),
            min_held_cents=min((p for _,p in held),default=None), closed_early=closing.get('status')=='filled', events_held=events))

def summarize(rows):
    measured=[t for t in rows if t['quote_samples']>=2]
    avg=lambda k:round(statistics.mean(t[k] for t in measured if t[k] is not None),3) if any(t[k] is not None for t in measured) else None
    return dict(trades=len(rows), measured=len(measured), wins=sum(t['outcome']=='win' for t in rows), losses=sum(t['outcome']=='loss' for t in rows),
        net=round(sum(t['pnl'] for t in rows),4), crosses_held=sum(t['crosses_held'] for t in measured),
        mean_crosses_held=avg('crosses_held'), median_crosses_held=statistics.median([t['crosses_held'] for t in measured]) if measured else None,
        crosses_round=sum(t['crosses_round'] for t in measured),mean_crosses_round=avg('crosses_round'),
        zero_crosses=sum(t['crosses_held']==0 for t in measured),any_crosses=sum(t['crosses_held']>0 for t in measured),
        ever_below50=sum(t['ever_below50_held'] for t in measured),last_cross_up=sum(t['last_cross_direction']=='up' for t in measured),
        last_cross_down=sum(t['last_cross_direction']=='down' for t in measured),mean_entry_minute=avg('entry_minute'),
        early_entry=sum(t['entry_minute']<3 for t in rows), mean_held_minutes=avg('held_minutes'),
        mean_confidence=avg('confidence'),mean_entry_cents=avg('entry_cents'),closed_early=sum(t['closed_early'] for t in rows),
        stop_disabled=sum(t['stop_enabled'] is False for t in rows),final_agrees=sum(t['final_agrees'] for t in rows),
        mean_crosses_before_entry=avg('crosses_before_entry'),mean_crosses_last60_before_entry=avg('crosses_last60_before_entry'),
        wide_crosses_held=sum(t['wide_crosses_held'] for t in measured),mean_wide_crosses_held=avg('wide_crosses_held'))

groups={}
for label, key in [('held_crossings',lambda t:'0' if t['crosses_held']==0 else '1-2' if t['crosses_held']<=2 else '3-5' if t['crosses_held']<=5 else '6+'),
                   ('before_entry_crossings',lambda t:'0' if t['crosses_before_entry']==0 else '1-2' if t['crosses_before_entry']<=2 else '3+'),
                   ('entry_time',lambda t:'0-3' if t['entry_minute']<3 else '3-5' if t['entry_minute']<5 else '5+'),
                   ('rule',lambda t:t['rule']),('side',lambda t:t['side']),('final_agrees',lambda t:str(t['final_agrees'])),
                   ('date',lambda t:t['round_start'][:10]),('closed_early',lambda t:str(t['closed_early']))]:
    values=sorted(set(key(t) for t in trades))
    groups[label]={v:summarize([t for t in trades if key(t)==v]) for v in values}
report=dict(cutoff=cutoff.isoformat(),asof=now.isoformat(),source_sha256=hashlib.sha256(raw).hexdigest(),
    methodology='Strict side changes <50 to >50 or reverse; exact 50 touches ignored; no artificial settlement or entry samples. Holding interval uses submitted_at through filled close or round end. Quotes are observed contract series, not necessarily executable bids. No may be derived from Yes. Counts are lower bounds when quotes are missing. Each filled order counted once, regardless of contract quantity.',
    excluded=dict(excluded),summary=summarize(trades),outcomes={v:summarize([t for t in trades if t['outcome']==v]) for v in ('win','loss','flat')},groups=groups,trades=trades)
out=ROOT/'reports/contract_50_crossings_since_20261103.json'
out.write_text(json.dumps(report,indent=2))
print(json.dumps({k:report[k] for k in ('cutoff','asof','excluded','summary','outcomes','groups')},indent=2))
