"""Conservative live calibration from past observed rounds, gated by holdout scores."""
import math
import json
from datetime import datetime, timedelta
import numpy as np
import price_history

FEATURES = ('baseline_logit', 'gap_over_range', 'range_pct', 'move_imbalance',
            'reversal_rate', 'minute_momentum', 'round_progress')
MIN_ROUNDS = 40
MAX_ROUNDS = 300
BLEND = .25


def finite(value):
    return isinstance(value, (int,float)) and not isinstance(value,bool) and math.isfinite(value)


def features(points, target, stamp, start, end, base):
    observed = [p for p in points if datetime.fromisoformat(p['observed_at']) <= stamp]
    if not observed or not finite(target) or target <= 0 or not finite(base):
        return None
    last = observed[-1]
    if not 0 <= (stamp-datetime.fromisoformat(last['observed_at'])).total_seconds() <= 20:
        return None
    metric = price_history.volatility_60s(observed,datetime.fromisoformat(last['observed_at']))
    if metric['status'] != 'ready' or not start <= stamp < end:
        return None
    window = [p for p in observed if (datetime.fromisoformat(last['observed_at'])-datetime.fromisoformat(p['observed_at'])).total_seconds() <= 60]
    spread = max(metric['range_dollars'],target*.00001)
    remaining = max(1,(end-stamp).total_seconds()/60)
    moves = metric['up_moves']+metric['down_moves']
    base = float(np.clip(base,.02,.98))
    vector = [math.log(base/(1-base)), np.clip((last['price']-target)/(spread*math.sqrt(remaining)),-8,8),
              metric['range_pct'], (metric['up_moves']-metric['down_moves'])/max(1,moves),
              metric['direction_reversals']/max(1,moves-1),
              (last['price']-window[0]['price'])/spread,(stamp-start).total_seconds()/(end-start).total_seconds()]
    return np.asarray(vector,dtype=float),metric


def fit(examples):
    x=np.asarray([e['x'] for e in examples]);y=np.asarray([e['y'] for e in examples])
    mean=x.mean(axis=0);scale=np.maximum(x.std(axis=0),.05)
    z=np.column_stack([np.ones(len(x)),(x-mean)/scale])
    # Each round carries equal total weight, regardless of available samples.
    counts={key:sum(e['round']==key for e in examples) for key in {e['round'] for e in examples}}
    weights=np.asarray([1/counts[e['round']] for e in examples]);weights/=weights.sum()
    beta=np.zeros(z.shape[1])
    offsets=np.asarray([math.log(np.clip(e['base'],.02,.98)/(1-np.clip(e['base'],.02,.98))) for e in examples])
    for _ in range(300):
        p=1/(1+np.exp(-np.clip(offsets+z@beta,-20,20)))
        penalty=.1*beta;penalty[0]=0
        beta-=.15*(z.T@((p-y)*weights)+penalty)
    return mean,scale,beta


def predict(model, x, base):
    mean,scale,beta=model
    logit=math.log(np.clip(base,.02,.98)/(1-np.clip(base,.02,.98)))
    correction=np.r_[1,(x-mean)/scale]@beta
    learned=1/(1+math.exp(-float(np.clip(logit+correction,-20,20))))
    return float(np.clip((1-BLEND)*base+BLEND*learned,.02,.98))


def scores(examples, probabilities):
    grouped={}
    for e,p in zip(examples,probabilities):
        p=float(np.clip(p,.0001,.9999));y=e['y']
        grouped.setdefault(e['round'],[]).append(((p-y)**2,-y*math.log(p)-(1-y)*math.log(1-p),int((p>=.5)==bool(y))))
    means=[np.mean(values,axis=0) for values in grouped.values()]
    result=np.mean(means,axis=0)
    return {'rounds':len(grouped),'samples':len(examples),'brier':float(result[0]),'log_loss':float(result[1]),'accuracy':float(result[2])}


def recorded_baselines(asset, start, end, target):
    """Recover pre-learning Live probabilities actually available during the round."""
    path = price_history.path_for(asset, start).with_suffix('.live.jsonl')
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            stamp = datetime.fromisoformat(row['updated_at'])
            base = (row.get('learning') or {}).get('base_p_up', row.get('p_up'))
            if (start <= stamp < end and finite(base) and 0 <= base <= 1
                    and finite(row.get('current_price')) and finite(row.get('target_gap'))
                    and abs(row['current_price']-row['target_gap']-target) < .001):
                records.append((stamp, base))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(records)


class LiveLearner:
    def __init__(self):
        self.key=None
        self.model=None
        self.info={'status':'insufficient'}

    def train(self, asset, start, rows, baseline):
        self.model=None
        usable=[];excluded=0
        recorded_samples=replayed_samples=0
        candidates=[]
        for row in rows:
            try:
                if (row.get('asset',asset)==asset and row.get('status')=='settled'
                        and datetime.fromisoformat(row['round_end'])<=start):
                    candidates.append(row)
            except (KeyError,TypeError,ValueError):
                continue
        for row in sorted(candidates,key=lambda r:datetime.fromisoformat(r['round_start']))[-MAX_ROUNDS:]:
            try:
                first,last=datetime.fromisoformat(row['round_start']),datetime.fromisoformat(row['round_end'])
                if last > start or row.get('status')!='settled' or row.get('actual') not in ('UP / ABOVE','DOWN / BELOW'):
                    continue
                target,settlement,prior=row.get('target'),row.get('settlement'),row.get('p_up')
                if not all(finite(v) for v in (target,settlement,prior)) or target<=0 or not 0<=prior<=1:
                    excluded+=1;continue
                points=price_history.load(asset,first,last)
                if points and abs(target-points[0]['price'])>max(target*.005,3*(max(p['price'] for p in points)-min(p['price'] for p in points))):
                    excluded+=1;continue
                tail=[p['price'] for p in points if (last-datetime.fromisoformat(p['observed_at'])).total_seconds()<=60]
                # Reject labels incompatible with the recorded settlement-minute prices.
                if len(tail)<4 or settlement<min(tail)-2 or settlement>max(tail)+2 or (settlement>=target)!=(row['actual']=='UP / ABOVE'):
                    excluded+=1;continue
                recorded = recorded_baselines(asset, first, last, target)
                samples=[]
                for minute in (2,5,8,11,14):
                    point=next((p for p in points if minute<=p['minute']<minute+.25),None)
                    if not point:continue
                    stamp=datetime.fromisoformat(point['observed_at'])
                    info={'start':first,'end':last,'target':target,'p_up':prior,
                          'start_reference':points[0]['price'],'prediction':row['prediction']}
                    available = [(when, value) for when, value in recorded if 0 <= (stamp-when).total_seconds() <= 20]
                    if available:
                        base = available[-1][1]
                        recorded_samples += 1
                    else:
                        base = baseline(info,point['price'],stamp)
                        replayed_samples += 1
                    output=features(points,target,stamp,first,last,base)
                    if output:
                        samples.append({'round':first.isoformat(),'x':output[0],'y':int(row['actual']=='UP / ABOVE'),'base':base})
                if samples:usable.append(samples)
                else:excluded+=1
            except (KeyError,ValueError,TypeError,OverflowError):
                excluded+=1
        self.info={'status':'insufficient','usable_rounds':len(usable),'excluded_rounds':excluded,
                   'minimum_rounds':MIN_ROUNDS,'features':list(FEATURES),'blend':BLEND,
                   'recorded_samples':recorded_samples,'replayed_samples':replayed_samples}
        if len(usable)<MIN_ROUNDS:return
        cut=int(len(usable)*.7)
        train=[e for group in usable[:cut] for e in group];holdout=[e for group in usable[cut:] for e in group]
        self.model=fit(train)
        old=scores(holdout,[e['base'] for e in holdout])
        new=scores(holdout,[predict(self.model,e['x'],e['base']) for e in holdout])
        supported=new['rounds']>=10 and new['brier']<old['brier']-.002 and new['log_loss']<old['log_loss'] and new['accuracy']>=old['accuracy']
        self.info.update(status='active' if supported else 'shadow',baseline=old,candidate=new,
                         training_rounds=cut,validation_rounds=len(usable)-cut,
                         training_end=usable[cut-1][0]['round'],validation_start=usable[cut][0]['round'])

    def apply(self, recommendation, points, start, end, strength):
        result=dict(recommendation)
        base=result['p_up']
        info=dict(self.info,base_p_up=base)
        output=features(points,result['current_price']-result['target_gap'],result['updated_at'],start,end,base)
        if output is None:
            info['applied']=False;info['reason']='insufficient_or_stale_window'
        else:
            vector,metric=output
            info.update(applied=False,window=metric)
            if self.model is not None:
                candidate=predict(self.model,vector,base)
                info['candidate_p_up']=candidate
                if self.info['status']=='active':
                    result.update(p_up=candidate,p_down=1-candidate,confidence=max(candidate,1-candidate),strength=strength(candidate))
                    direction='UP / ABOVE' if candidate>=.5 else 'DOWN / BELOW'
                    changed=direction!=result['original_prediction']
                    result.update(recommendation=direction,changed=changed,status='CHANGED' if changed else 'STILL THE SAME')
                    info['applied']=True
        result['learning']=info
        return result


def main():
    """Read-only historical replay; never starts the trading browser."""
    import argparse
    import json
    from datetime import timezone
    from pathlib import Path
    import brti_engine as core
    import round_history
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset',default='BTC')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    now=datetime.now(timezone.utc)
    rows=round_history.load(args.asset)
    completed=sorted([r for r in rows if r.get('status')=='settled' and datetime.fromisoformat(r['round_end'])<=now],key=lambda r:datetime.fromisoformat(r['round_start']))[-MAX_ROUNDS:]
    if not completed:
        report={'status':'insufficient','usable_rounds':0}
    else:
        first=datetime.fromisoformat(completed[0]['round_start'])
        hours=(now-first).total_seconds()/3600+core.DEFAULT_CONTEXT_HOURS+2
        index=core.CandleIndex(core.fetch_recent_candles(hours,args.asset))
        learner=LiveLearner()
        learner.train(args.asset,now,completed,lambda info,value,stamp:core.build_live_recommendation(
            info,value,stamp,index,core.DEFAULT_CONTEXT_HOURS)['p_up'])
        report=learner.info
    output=json.dumps(report,indent=2,allow_nan=False)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(output)
    print(output)


if __name__=='__main__':
    main()
