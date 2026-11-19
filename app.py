from flask import Flask, jsonify, request, Response
from types import SimpleNamespace
from collections import deque
from datetime import datetime, timedelta
import threading
import time
import math
import json
import gzip
from pathlib import Path
import robinhood_target
import round_history
import choppiness
import price_history
import prospective_study
import entry_stability
import logging
import buy_analysis
import live_learning
import frozen_learning
import live_market_support
import order_settings
import order_logs

import numpy as np

import brti_engine as core


app = Flask(__name__)

ARGS = SimpleNamespace(
    context_hours=core.DEFAULT_CONTEXT_HOURS,
    pattern_days=core.DEFAULT_PATTERN_DAYS,
    backtest_hours=core.DEFAULT_BACKTEST_HOURS,
    neighbors=core.DEFAULT_K_NEIGHBORS,
    simulations=core.DEFAULT_SIMULATIONS,
    backtest_simulations=core.DEFAULT_BACKTEST_SIMULATIONS,
    weight_prior_rounds=core.DEFAULT_WEIGHT_PRIOR_ROUNDS,
    weight_base_score=core.DEFAULT_WEIGHT_BASE_SCORE,
    live_refresh=core.DEFAULT_LIVE_REFRESH_SECONDS,
    target_mode="manual",
    source="auto",
    asset=core.DEFAULT_ASSET,
    browser_headless=False,
)

SOURCE = core.BenchmarkSource("auto", ARGS.asset)
STATS = core.load_stats(ARGS.asset)

ORDER_SETTINGS = order_settings.load()

LOCK = threading.RLock()
HISTORY_READY = threading.Event()
STOP_EVENT = threading.Event()

HISTORY = {
    "candles": None,
    "index": None,
    "records": None,
    "updated_at": None,
    "loading": False,
    "error": None,
}

STATE = {
    "round_start": None,
    "round_end": None,
    "target": None,
    "target_quality": "MANUAL Robinhood target",
    "robinhood_status": "Starting Robinhood browser...",
    "order_status_logs": deque(maxlen=500),
    "order_status_log_id": 0,
    "order_log_archive_error": None,
    "force_buy_request": None,
    "contract_prices": None,
    "available_balance": None,
    "prediction": None,
    "live_recommendation": None,
    "recommendation_minute": None,
    "analysis_status": "waiting_target",
    "analysis_error": None,
    "live_price": None,
    "live_updated_at": None,
    "exchange_quotes": {},
    "exchange_errors": {},
    "samples": deque(maxlen=180),
    "underlying_prices": deque(maxlen=2000),
    "latest_settlement": None,
    "history_message": "Loading historical data...",
}

LIVE_LEARNER = live_learning.LiveLearner()
ANALYSIS_TOKEN = 0


def now_local():
    return datetime.now().astimezone()


def dt_iso(dt):
    return dt.isoformat() if dt else None


def money(v):
    return None if v is None else round(float(v), 2)


def current_round_key():
    return core.current_round()


def prediction_to_json(p):
    if not p:
        return None

    bt = p["backtest"]

    models = []
    mapping = [
        ("pattern", "Pattern", p["p_pattern"]),
        ("regime", "Recent", p["p_regime"]),
        ("bootstrap", "Bootstrap", p["p_bootstrap"]),
        ("trend", "Trend", p["p_trend"]),
    ]

    for key, label, probability in mapping:
        w = p["adaptive_weights"][key]
        models.append({
            "key": key,
            "label": label,
            "probability_up": round(probability * 100.0, 2),
            "weight": round(w["weight"] * 100.0, 2),
            "bt_accuracy": round(w["smoothed_accuracy"] * 100.0, 2),
        })

    return {
        "target": money(p["target"]),
        "start_reference": money(p["start_reference"]),
        "required_move_dollars": round(p["required_move_dollars"], 2),
        "required_return_pct": round(p["required_return"] * 100.0, 4),
        "p_up": round(p["p_up"] * 100.0, 2),
        "p_down": round(p["p_down"] * 100.0, 2),
        "prediction": p["prediction"],
        "confidence": round(p["confidence"] * 100.0, 2),
        "strength": p["strength"],
        "agreement": p["component_agreement"],
        "learning": p.get("frozen_learning"),
        "models": models,
        "backtest": {
            "rounds": bt["rounds"],
            "accuracy": round(bt["accuracy_pct"], 2),
            "hits": bt["hits"],
            "misses": bt["misses"],
            "avg_confidence": round(bt["avg_confidence_pct"], 2),
            "brier": None if bt["brier"] is None else round(bt["brier"], 4),
            "calibration_samples": p["calibration_samples"],
        },
        "moves": {
            "p10": round(p["neighbor_p10"] * 100.0, 4),
            "median": round(p["neighbor_median_move"] * 100.0, 4),
            "p90": round(p["neighbor_p90"] * 100.0, 4),
        },
    }


def recommendation_to_json(rec):
    if not rec:
        return None

    return {
        "updated_at": dt_iso(rec["updated_at"]),
        "minute": rec["minute"],
        "remaining_minutes": rec["remaining_minutes"],
        "current_price": money(rec["current_price"]),
        "change_dollars": round(rec["change_dollars"], 2),
        "change_pct": round(rec["change_pct"], 4),
        "target_gap": round(rec["target_gap"], 2),
        "p_up": round(rec["p_up"] * 100.0, 2),
        "p_down": round(rec["p_down"] * 100.0, 2),
        "recommendation": rec["recommendation"],
        "confidence": round(rec["confidence"] * 100.0, 2),
        "strength": rec["strength"],
        "changed": rec["changed"],
        "status": rec["status"],
        "original_prediction": rec["original_prediction"],
        "learning": rec.get("learning"),
        "market_support": rec.get("market_support"),
    }


_DASHBOARD_STATS_CACHE = {}


def dashboard_stats_json():
    """Reuse display-only aggregates for one second; trade guards stay live."""
    key = (ARGS.asset, round_history.ROOT, dt_iso(STATE['round_start']),
           json.dumps(ORDER_SETTINGS, sort_keys=True), stats_json)
    now = time.monotonic()
    if (_DASHBOARD_STATS_CACHE.get('key') != key
            or now - _DASHBOARD_STATS_CACHE['updated_at'] >= 1):
        value = stats_json()
        _DASHBOARD_STATS_CACHE.update(key=key, value=value, updated_at=time.monotonic())
    return _DASHBOARD_STATS_CACHE['value']


def stats_json():
    records = round_history.merged(ARGS.asset, STATS.get("rounds", []), include_quotes=False)
    stats = round_history.purchase_accuracy(records)
    stats["final_prediction"] = round_history.final_prediction_accuracy(records)
    stats["buy_accuracy_guard"] = round_history.accuracy_buy_guard(records, ORDER_SETTINGS, now_local())
    stats["buy_choppiness_guard"] = current_buy_choppiness_guard(include_current=True)
    stats["balance"] = round_history.balance_summary(records, ORDER_SETTINGS.get("initial_balance", 0))
    return stats


def journal_round(status=None, **fields):
    # Called under LOCK when state may be modified by another worker.
    start, end = STATE["round_start"], STATE["round_end"]
    if start is None or end is None:
        return
    if status:
        fields["status"] = status
    return round_history.upsert(ARGS.asset, dt_iso(start), dt_iso(end), **fields)


def settle_previous_round(previous_end):
    with LOCK:
        p = STATE["prediction"]
        target = STATE["target"]
        samples = list(STATE["samples"])

    if not p or target is None:
        with LOCK:
            journal_round("no_prediction", error=STATE["analysis_error"])
        return

    window_start = previous_end - timedelta(seconds=60)

    values = [
        price
        for ts, price in samples
        if window_start <= ts < previous_end
    ]

    if not values:
        with LOCK:
            journal_round("unsettled", error="No price samples in the final minute.")
        return

    settlement = float(np.mean(values))
    actual = "UP / ABOVE" if settlement >= target else "DOWN / BELOW"
    hit = actual == p["prediction"]

    core.record_result(
        STATS,
        p,
        settlement,
        actual,
        hit,
    )

    with LOCK:
        journal_round("settled", settlement=settlement, actual=actual, hit=bool(hit),
                      component_results=core.component_round_results(p, actual), error=None)
        STATE["latest_settlement"] = {
            "round_start": dt_iso(p["start"]),
            "round_end": dt_iso(p["end"]),
            "target": money(target),
            "settlement": money(settlement),
            "sample_count": len(values),
            "actual": actual,
            "prediction": p["prediction"],
            "confidence": round(p["confidence"] * 100.0, 2),
            "hit": bool(hit),
        }


def refresh_history(force=False):
    if HISTORY["loading"] and not force:
        return

    requested_asset = ARGS.asset
    HISTORY_READY.clear()

    with LOCK:
        HISTORY["loading"] = True
        HISTORY["error"] = None
        STATE["history_message"] = "Refreshing multi-exchange history..."

    try:
        candles, index, records = core.load_model_history(ARGS)

        with LOCK:
            if requested_asset != ARGS.asset:
                return
            HISTORY["candles"] = candles
            HISTORY["index"] = index
            HISTORY["records"] = records
            HISTORY["updated_at"] = now_local()
            HISTORY["loading"] = False
            HISTORY["error"] = None
            STATE["history_message"] = (
                f"History ready: {len(candles)} candles / "
                f"{len(records)} pattern rounds"
            )

        HISTORY_READY.set()

    except Exception as exc:
        with LOCK:
            HISTORY["loading"] = False
            HISTORY["error"] = str(exc)
            STATE["history_message"] = f"History error: {exc}"

        HISTORY_READY.set()


def start_history_refresh():
    with LOCK:
        if HISTORY["loading"]:
            return

    threading.Thread(
        target=refresh_history,
        daemon=True,
    ).start()


def analyze_target(target, round_start, round_end, token, quality="MANUAL Robinhood target"):
    try:
        HISTORY_READY.wait(timeout=120)

        with LOCK:
            if HISTORY["error"]:
                raise RuntimeError(HISTORY["error"])

            index = HISTORY["index"]
            records = HISTORY["records"]

        if index is None or records is None:
            raise RuntimeError("Historical data is not ready.")

        prediction = core.build_round_prediction(
            index=index,
            records=records,
            start=round_start,
            end=round_end,
            target=float(target),
            target_quality=quality,
            args=ARGS,
        )

        prediction = frozen_learning.apply(prediction,
            round_history.load(ARGS.asset, include_quotes=False), ARGS.asset, core.confidence_strength)

        with LOCK:
            if token != globals()["ANALYSIS_TOKEN"]:
                return

            current_start, _ = core.current_round()
            if current_start != round_start:
                return

            STATE["target"] = float(target)
            STATE["prediction"] = prediction
            STATE["live_recommendation"] = None
            STATE["recommendation_minute"] = None
            STATE["analysis_status"] = "ready"
            STATE["analysis_error"] = None
            components = core.component_round_results(prediction, None)
            for item in components.values():
                item["hit"] = None
            journal_round("predicted", target=float(target), prediction=prediction["prediction"],
                          p_up=prediction["p_up"], p_down=prediction["p_down"],
                          component_results=components,
                          adaptive_weights={key: item["weight"] for key, item in prediction.get("adaptive_weights", {}).items()},
                          target_quality=quality, frozen_learning=prediction.get("frozen_learning"), error=None)

    except Exception as exc:
        with LOCK:
            if token == globals()["ANALYSIS_TOKEN"]:
                STATE["analysis_status"] = "error"
                STATE["analysis_error"] = str(exc)
                journal_round("error", error=str(exc))


def robinhood_snapshot():
    with LOCK:
        start, end = current_round_key()
        has_target = STATE["round_start"] == start and STATE["target"] is not None
        return ARGS.asset, start, end, has_target, False


def robinhood_report(message):
    with LOCK:
        STATE["robinhood_status"] = message
        entry={"timestamp":datetime.now().astimezone().isoformat(),"message":str(message)}
        try:
            entry['id']=order_logs.append(entry['timestamp'],entry['message'])
            STATE['order_status_log_id']=entry['id']
            STATE['order_log_archive_error']=None
        except (OSError, order_logs.sqlite3.Error) as exc:
            STATE['order_status_log_id']+=1
            STATE['order_log_archive_error']=str(exc)
        STATE['order_status_logs'].append(entry)


def accept_robinhood_target(target, asset, start, end, url):
    global ANALYSIS_TOKEN
    with LOCK:
        current_start, current_end = current_round_key()
        if ARGS.asset != asset:
            return False
        if (current_start, current_end) != (start, end):
            return False
        # Wait for live_worker to settle/reset the previous round first.
        if STATE["round_start"] != start or STATE["target"] is not None:
            return False
        saved = {"asset": asset, "round_start": dt_iso(start),
                 "round_end": dt_iso(end), "target": target, "source_url": url}
        path = Path(__file__).resolve().parent / "current_target.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(saved, indent=2) + "\n")
        temp.replace(path)
        ANALYSIS_TOKEN += 1
        token = ANALYSIS_TOKEN
        STATE.update(target=target, target_quality="AUTO Robinhood browser target",
                     prediction=None, live_recommendation=None, recommendation_minute=None,
                     analysis_status="analyzing", analysis_error=None)
        robinhood_report(f"Recorded {asset} target {target:,.2f} for this round.")
        journal_round("analyzing", target=target, target_quality="AUTO Robinhood browser target", source_url=url)
    threading.Thread(target=analyze_target,
                     args=(target, start, end, token, "AUTO Robinhood browser target"),
                     daemon=True).start()
    return True


def current_buy_accuracy_guard():
    if not ORDER_SETTINGS.get('accuracy_buy_guard', {}).get('enabled'):
        return {'enabled': False, 'allowed': True}
    records = round_history.merged(ARGS.asset, STATS.get('rounds', []), include_quotes=False)
    return round_history.accuracy_buy_guard(records, ORDER_SETTINGS, now_local())


def round_choppiness(asset, start, end, asof, source):
    if source == 'bitcoin':
        row = round_history.get('BTC', start.isoformat(), include_quotes=False) or {}
        return price_history.round_choppiness(start, end, asof, row.get('target'))
    if source == 'contract_yes':
        return round_history.contract_round_choppiness(asset, start, end, asof)
    raise ValueError('Choppiness source must be bitcoin or contract_yes.')


def current_buy_choppiness_guard(include_current=False):
    config = dict(order_settings.CHOPPINESS_BUY_GUARD_DEFAULTS, **ORDER_SETTINGS.get('choppiness_buy_guard', {}))
    if not any(config.get(key) for key in ('enabled', 'current_enabled', 'block_next_round')) and not include_current:
        return {'enabled': False, 'allowed': True}
    now = now_local()
    start, end = core.current_round(now)
    scores = []
    # Exactly the last N consecutive completed rounds, including unbought ones.
    # Do not skip a missing round or use an older round in its place.
    for lag in range(config.get('rounds', 3)):
        previous_end = start - timedelta(minutes=15 * lag)
        previous_start = previous_end - timedelta(minutes=15)
        scores.append(round_choppiness(ARGS.asset, previous_start, previous_end, now, config['source']))
    current = round_choppiness(ARGS.asset, start, end, now, config['source'])
    guard = choppiness.buy_guard(scores, config, current)
    guard['blocked_until'] = dt_iso(end) if not guard['allowed'] else None
    if include_current:
        guard['current_round'] = current
    return guard


def robinhood_form_prediction(asset, start, end):
    with LOCK:
        prediction = STATE["prediction"]
        if (ARGS.asset != asset or current_round_key() != (start, end)
                or STATE["round_start"] != start or not prediction
                or prediction["start"] != start or prediction["end"] != end):
            return None
        directions = {"Final prediction": prediction["prediction"]}
        for label, key in (("Pattern", "p_pattern"), ("Recent", "p_regime"),
                           ("Bootstrap", "p_bootstrap"), ("Trend", "p_trend")):
            value = prediction.get(key)
            directions[label] = ("UP / ABOVE" if value >= 0.5 else "DOWN / BELOW") if value is not None and math.isfinite(value) else None
        rec = STATE["live_recommendation"]
        live_direction = None
        live_snapshot = None
        if rec and rec.get("updated_at"):
            age = (now_local() - rec["updated_at"]).total_seconds()
            quote_time = getattr(SOURCE, 'last_quote_at', None) if getattr(SOURCE, 'mode', None) == 'cfbenchmarks' else None
            quote_fresh = quote_time is None or 0 <= (now_local()-quote_time).total_seconds() <= core.MAX_QUOTE_AGE_SECONDS
            if quote_fresh and 0 <= age <= core.LIVE_RECOMMENDATION_REFRESH_SECONDS * 2:
                live_direction = rec.get("recommendation")
                live_snapshot = {"direction": live_direction, "p_up": rec.get("p_up"),
                                 "confidence": rec.get("confidence"), "minute": rec.get("minute"),
                                 "updated_at": dt_iso(rec["updated_at"]), "learning": rec.get("learning"),
                                 "market_support": rec.get("market_support")}
        row = (round_history.get(asset,dt_iso(start)) or {})
        brti = None
        updated = STATE['live_updated_at']
        if (asset == 'BTC' and updated
                and 0 <= (now_local() - updated).total_seconds() <= core.MAX_QUOTE_AGE_SECONDS
                and (getattr(SOURCE, 'last_quote_at', None) is None
                     or 0 <= (now_local()-SOURCE.last_quote_at).total_seconds() <= core.MAX_QUOTE_AGE_SECONDS)):
            brti = STATE['exchange_quotes'].get('CF Benchmarks BRTI')
        return {"direction": prediction["prediction"], "target": STATE["target"],
                "model_directions": directions, "live_direction": live_direction,
                "live_snapshot": live_snapshot, "brti_price": brti,
                "entry_stability": entry_stability.measure(STATE["underlying_prices"], start, end, now_local(), STATE["target"]),
                "contract_price_history": current_contract_price_history(start),
                "contract_prices": current_contract_prices(start),
                "purchase_exists": bool(row.get("purchase")) and (
                    not ORDER_SETTINGS.get('allow_multi_buy', True)
                    or (row['purchase'].get('close_order') or {}).get('status') != 'filled'),
                "purchase": dict(row.get('purchase') or {}),
                "force_buy_request": current_force_buy_request(asset, start, end),
                "purchase_removal_revision": row.get('purchase_removal_revision', 0),
                "buy_accuracy_guard": current_buy_accuracy_guard(),
                "buy_choppiness_guard": current_buy_choppiness_guard(),
                "order_settings": dict(ORDER_SETTINGS)}


def current_force_buy_request(asset, start, end):
    pending = STATE.get('force_buy_request')
    if (pending and pending['asset'] == asset and pending['round_start'] == dt_iso(start)
            and pending.get('analysis_token') == ANALYSIS_TOKEN
            and start <= now_local() < end and ORDER_SETTINGS.get('force_buy', {}).get('enabled')
            and (now_local()-start).total_seconds()/60 < ORDER_SETTINGS['force_buy']['minute_to']):
        return pending['requested_at']
    return None


def record_robinhood_purchase(asset, start, end, purchase):
    with LOCK:
        saved = _record_robinhood_purchase(asset, start, end, purchase)
        if saved and purchase.get('status') == 'attempted':
            pending = STATE.get('force_buy_request')
            if pending and pending['asset'] == asset and pending['round_start'] == dt_iso(start):
                STATE['force_buy_request'] = None
        return saved


def _record_robinhood_purchase(asset, start, end, purchase):
    with LOCK:
        if purchase.get('status') == 'attempted' and (ORDER_SETTINGS.get('require_final_live_agreement') or ORDER_SETTINGS.get('min_model_agreement')):
            snapshot = robinhood_form_prediction(asset, start, end)
            if not robinhood_target.entry_consensus_allows(snapshot, purchase.get('direction')):
                return False
        if purchase.get('status') == 'attempted':
            guard = current_buy_accuracy_guard()
            virtual_attempt = purchase.get('test_mode') is True and robinhood_target.effective_test_mode(
                {'order_settings': ORDER_SETTINGS, 'buy_accuracy_guard': guard})
            if not virtual_attempt and not guard['allowed']:
                return False
        if purchase.get('status') == 'attempted' and not current_buy_choppiness_guard()['allowed']:
            return False
        existing = round_history.get(asset,dt_iso(start))
        if not existing:
            return False
        if existing.get("purchase_removed") and purchase.get('status') != 'attempted':
            return True
        old = existing.get("purchase") or {}
        if purchase.get('cancellation_reason') == 'ioc_cancelled_unfilled':
            if (old.get('status') not in ('attempted', 'submitted') or old.get('test_mode')
                    or (old.get('conditions') or {}).get('time_in_force') != 'ioc'
                    or old.get('target') != purchase.get('target')
                    or old.get('attempted_at') != purchase.get('expected_attempted_at')
                    or old.get('submitted_at') != purchase.get('expected_submitted_at')
                    or purchase.get('filled_quantity') != 0
                    or purchase.get('receipt_contracts') != old.get('contracts')
                    or purchase.get('confirmed_by') != 'visible_cancelled_ioc_receipt'):
                return False
            try:
                stamps = [datetime.fromisoformat(old[key]) for key in ('attempted_at','submitted_at') if old.get(key)]
                if not stamps or (now_local()-max(stamps)).total_seconds() < 10:
                    return False
            except (ValueError, TypeError):
                return False
            update = {key:value for key,value in purchase.items() if not key.startswith('expected_')}
            round_history.upsert(asset,dt_iso(start),dt_iso(end),purchase=dict(old,**update))
            return True
        if 'ioc_cleanup_refreshed_at' in purchase:
            if (old.get('status') != 'cancelled' or old.get('cancellation_reason') != 'ioc_cancelled_unfilled'
                    or old.get('target') != purchase.get('target')
                    or old.get('attempted_at') != purchase.get('expected_attempted_at')
                    or old.get('submitted_at') != purchase.get('expected_submitted_at')):
                return False
            archived = dict(old, ioc_cleanup_pending=False,
                            ioc_cleanup_refreshed_at=purchase['ioc_cleanup_refreshed_at'])
            round_history.upsert(asset,dt_iso(start),dt_iso(end),purchase=None,
                purchase_removed=True,removed_purchase=archived,
                purchase_removal_revision=existing.get('purchase_removal_revision',0)+1)
            _DASHBOARD_STATS_CACHE.clear()
            return True
        if purchase.get('cancellation_reason') == 'attempt_confirmation_timeout':
            if (old.get('status') != 'attempted' or old.get('target') != purchase.get('target')
                    or old.get('attempted_at') != purchase.get('expected_attempted_at')):
                return False
            try:
                if (now_local()-datetime.fromisoformat(old['attempted_at'])).total_seconds() < 30:
                    return False
            except (KeyError, TypeError, ValueError):
                return False
            update={key:value for key,value in purchase.items() if key!='expected_attempted_at'}
            round_history.upsert(asset,dt_iso(start),dt_iso(end),purchase=dict(old,**update))
            return True
        if 'timeout_refreshed_at' in purchase:
            if old.get('status') != 'cancelled' or old.get('cancellation_reason') != 'attempt_confirmation_timeout':
                return False
            archived = dict(old, timeout_refresh_pending=False,
                            timeout_refreshed_at=purchase['timeout_refreshed_at'])
            round_history.upsert(asset,dt_iso(start),dt_iso(end),purchase=None,
                purchase_removed=True,removed_purchase=archived,
                purchase_removal_revision=existing.get('purchase_removal_revision',0)+1)
            return True
        if purchase.get('status') == 'attempted' and (old.get('close_order') or {}).get('status') == 'filled':
            if not ORDER_SETTINGS.get('allow_multi_buy', True):
                return False
            cycles = list(existing.get('completed_purchases') or [])
            cycles.append(old)
            round_history.upsert(asset, dt_iso(start), dt_iso(end),
                                 completed_purchases=cycles, purchase=dict(purchase), purchase_removed=False)
            return True
        if purchase.get('close_order'):
            if old.get('status') != 'filled' or old.get('target') != purchase.get('target'):
                return False
            closing = purchase['close_order']
            if closing.get('status') == 'attempted' and (
                    closing.get('reason') == 'stop_loss' and (
                        old.get('ignore_stop_loss') or old.get('purchase_rule') == 'force_buy'
                        or (old.get('conditions') or {}).get('rule') == 'force_buy')
                    or closing.get('reason') == 'close_conditions' and (
                        old.get('ignore_early_profit') or old.get('purchase_rule') == 'force_buy'
                        or (old.get('conditions') or {}).get('rule') == 'force_buy')):
                return False
            previous = old.get('close_order') or {}
            if previous.get('status') == 'filled':
                return True
            if closing.get('status') == 'filled' and (
                    closing.get('filled_quantity') != previous.get('contracts')
                    or closing.get('receipt_contracts') != previous.get('contracts')):
                return False
            round_history.upsert(asset, dt_iso(start), dt_iso(end), purchase=dict(
                old, close_order=dict(previous, **closing)))
            return True
        if 'holding_quote' in purchase:
            quote = purchase['holding_quote']
            price = quote.get('bid_cents') if isinstance(quote, dict) else None
            token = old.get('attempted_at') or old.get('submitted_at')
            if (old.get('status') != 'filled' or old.get('target') != purchase.get('target')
                    or (old.get('close_order') or {}).get('status') == 'filled'
                    or not isinstance(quote, dict) or quote.get('direction') != old.get('direction')
                    or quote.get('purchase_token') != token
                    or not isinstance(price, (int, float)) or isinstance(price, bool)
                    or not math.isfinite(price) or not 0 <= price <= 100
                    or not isinstance(old.get('contracts'), int) or isinstance(old.get('contracts'), bool)
                    or old['contracts'] <= 0
                    or not isinstance(quote.get('available_contracts'), int)
                    or quote['available_contracts'] < old['contracts']):
                return False
            STATE['holding_quote'] = dict(quote, asset=asset, round_start=dt_iso(start),
                                          target=purchase['target'], updated_at=dt_iso(now_local()))
            return True
        if 'stop_loss_peak_bid_cents' in purchase:
            peak = purchase['stop_loss_peak_bid_cents']
            if (old.get('status') != 'filled' or old.get('target') != purchase.get('target')
                    or old.get('close_order') or not isinstance(peak, (int, float))
                    or isinstance(peak, bool) or not math.isfinite(peak) or not 0 < peak <= 100):
                return False
            previous_peak = old.get('stop_loss_peak_bid_cents')
            if isinstance(previous_peak, (int, float)) and peak <= previous_peak:
                return True
            updated = dict(old, stop_loss_peak_bid_cents=peak,
                           stop_loss_peak_bid_at=purchase.get('stop_loss_peak_bid_at'))
            round_history.upsert(asset, dt_iso(start), dt_iso(end), purchase=updated)
            return True
        if purchase.get('position_confirmed'):
            if (old.get('status') != 'filled' or old.get('target') != purchase.get('target')
                    or not old.get('contracts')
                    or purchase.get('available_contracts', 0) < old['contracts']):
                return False
            if purchase.get('position_direction') and purchase['position_direction'] != old.get('direction'):
                return False
            if 'execution_price_cents' in purchase:
                price = purchase['execution_price_cents']
                if (purchase['available_contracts'] != old['contracts']
                        or not isinstance(price, (int, float)) or isinstance(price, bool)
                        or not math.isfinite(price) or not 0 < price <= 100):
                    return False
            if (old.get('position_confirmed') and old.get('available_contracts') == purchase['available_contracts']
                    and ('execution_price_cents' not in purchase
                         or old.get('execution_price_cents') == purchase['execution_price_cents'])):
                return True
        if purchase.get("status") == "filled":
            if not old or old.get("target") != purchase.get("target"):
                return False
            if purchase.get("confirmed_by") == "visible_order_receipt":
                if (purchase.get("filled_quantity") != old.get("contracts")
                        or purchase.get("receipt_contracts") != old.get("contracts")):
                    return False
        if (old.get("status") == "filled" and purchase.get("confirmed_by") != "visible_order_receipt"
                and not purchase.get('position_confirmed')):
            return True
        round_history.upsert(asset, dt_iso(start), dt_iso(end), purchase=dict(old, **purchase),
                             purchase_removed=False)
        return True


def record_live_recommendation(recommendation):
    """Persist direction transitions, rather than the flag comparing against Final."""
    start, end = STATE["round_start"], STATE["round_end"]
    if start is None or end is None:
        return
    direction = recommendation.get("recommendation")
    if direction not in ("UP / ABOVE", "DOWN / BELOW"):
        return
    stamp = recommendation.get("updated_at") or now_local()
    if not start <= stamp < end:
        return
    row = (round_history.get(ARGS.asset,dt_iso(start)) or {})
    previous = row.get("last_live_direction")
    fields = {}
    if previous != direction:
        fields["last_live_direction"] = direction
        if previous in ("UP / ABOVE", "DOWN / BELOW"):
            fields["last_live_change_at"] = dt_iso(stamp)
            fields["last_live_change_minute"] = round((stamp - start).total_seconds() / 60, 2)
            for key in ("change_dollars", "change_pct"):
                value = recommendation.get(key)
                fields[f"last_live_{key}_at_change"] = (float(value) if isinstance(value, (int, float))
                                                       and not isinstance(value, bool) and math.isfinite(value) else None)
    if fields:
        journal_round(**fields)


def record_contract_prices(asset, start, end, target, prices):
    with LOCK:
        if ARGS.asset != asset or current_round_key() != (start, end):
            return False
        if prices is None:
            STATE["contract_prices"] = None
            return True
        if STATE["target"] is not None and STATE["target"] != target:
            STATE["contract_prices"] = None
            return False
        stamp = now_local()
        STATE["contract_prices"] = dict(prices, asset=asset, round_start=dt_iso(start),
                                        target=target, updated_at=dt_iso(stamp))
        if start <= stamp < end:
            row = (round_history.get(asset,dt_iso(start)) or {})
            fields = {}
            valid_quotes = all(isinstance(prices.get(key), (int, float))
                               and not isinstance(prices.get(key), bool)
                               and math.isfinite(prices[key])
                               for key in ("yes_cents", "no_cents"))
            if valid_quotes:
                samples = list(row.get("contract_price_history") or [])
                sample = {"observed_at": dt_iso(stamp),
                          "minute": round((stamp - start).total_seconds() / 60, 4),
                          "target": target, "yes_cents": prices["yes_cents"],
                          "no_cents": prices["no_cents"]}
                previous = samples[-1] if samples and samples[-1].get('target') == target else {}
                for side in ('yes', 'no'):
                    if round_history.crossed_above_50(previous.get(side + '_cents'), sample[side + '_cents']):
                        fields[f'last_{side}_cross_50_at'] = dt_iso(stamp)
                        fields[f'last_{side}_cross_50_minute'] = round((stamp-start).total_seconds()/60, 2)
                # Retain an unchanged quote periodically too: scoring at fixed
                # instants must distinguish a flat market from missing data.
                heartbeat = (not samples or (stamp - datetime.fromisoformat(samples[-1]['observed_at'])).total_seconds()
                             >= choppiness.SAMPLE_SECONDS)
                if heartbeat or any(samples[-1].get(key) != sample[key]
                                    for key in ("target", "yes_cents", "no_cents")):
                    samples.append(sample)
                    fields["contract_price_history"] = samples
            for side, key in (("up", "yes_cents"), ("down", "no_cents")):
                value = prices.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 50:
                    fields[f"last_{side}_above_50_at"] = dt_iso(stamp)
                    fields[f"last_{side}_above_50_minute"] = round((stamp - start).total_seconds() / 60, 2)
                    if not row.get(f"first_{side}_above_50_at"):
                        fields[f"first_{side}_above_50_at"] = dt_iso(stamp)
                        fields[f"first_{side}_above_50_minute"] = round((stamp - start).total_seconds() / 60, 2)
            if fields:
                round_history.upsert(asset, dt_iso(start), dt_iso(end), **fields)
        return True


def current_contract_prices(start):
    prices = STATE.get("contract_prices")
    if (not prices or prices["asset"] != ARGS.asset or prices["round_start"] != dt_iso(start)
            or prices["target"] != STATE["target"]):
        return None
    age = (now_local() - datetime.fromisoformat(prices["updated_at"])).total_seconds()
    return dict(prices) if 0 <= age <= 10 else None


def current_contract_price_history(start):
    if start is None:
        return []
    row = round_history.get(ARGS.asset,dt_iso(start)) or {}
    return [sample for sample in row.get("contract_price_history", [])
            if sample.get("target") == STATE["target"]]


def record_underlying_price(start, end, stamp, price):
    if (start is None or end is None or stamp is None or not start <= stamp < end
            or not isinstance(price,(int,float)) or isinstance(price,bool)
            or not math.isfinite(price) or price <= 0):
        return
    points=STATE['underlying_prices']
    if points and stamp <= datetime.fromisoformat(points[-1]['observed_at']):
        return
    point={'observed_at':dt_iso(stamp),'minute':(stamp-start).total_seconds()/60,'price':float(price)}
    point['volatility_60s']=price_history.volatility_60s(list(points)+[point],stamp)
    price_history.append(ARGS.asset,start,point)
    points.append(point)


def record_available_balance(amount):
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount) or amount < 0:
        return
    with LOCK:
        STATE['available_balance']={'amount':float(amount),'updated_at':dt_iso(now_local()),
                                    'source':'Robinhood available'}


def current_available_balance():
    balance=STATE.get('available_balance')
    if not balance:
        return None
    try:
        age=(now_local()-datetime.fromisoformat(balance['updated_at'])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None
    return dict(balance) if 0 <= age <= 30 else None


def robinhood_worker():
    try:
        robinhood_target.browser_loop(STOP_EVENT, robinhood_snapshot,
                                     accept_robinhood_target, robinhood_report, robinhood_form_prediction, record_robinhood_purchase, record_contract_prices,
                                     headless=ARGS.browser_headless, record_balance=record_available_balance)
    except Exception as exc:
        robinhood_report(f"Could not open Robinhood browser: {exc}")


def ensure_live_learner(asset, start, history_index):
    """Publish a baseline learner immediately; train a separate instance off the price loop."""
    global LIVE_LEARNER
    key = (asset, start)
    with LOCK:
        if LIVE_LEARNER.key == key:
            return LIVE_LEARNER
        pending = live_learning.LiveLearner()
        pending.key = key
        pending.info = {'status': 'training'}
        LIVE_LEARNER = pending
    context_hours = ARGS.context_hours

    def train():
        global LIVE_LEARNER
        trained = live_learning.LiveLearner()
        trained.key = key
        try:
            trained.train(asset, start, round_history.load(asset,include_quotes=False),
                          lambda info, value, stamp: core.build_live_recommendation(
                              info, value, stamp, history_index, context_hours)['p_up'])
        except Exception as exc:
            trained.model = None
            trained.info = {'status': 'error', 'reason': str(exc)}
        with LOCK:
            if LIVE_LEARNER is pending:
                LIVE_LEARNER = trained

    threading.Thread(target=train, daemon=True).start()
    return pending


def live_worker():
    global ANALYSIS_TOKEN

    start, end = current_round_key()

    with LOCK:
        STATE["round_start"] = start
        STATE["round_end"] = end
        STATE["underlying_prices"] = deque(price_history.load(ARGS.asset,start,end),maxlen=2000)
        journal_round("waiting_target")

    while not STOP_EVENT.is_set():
        loop_start = time.time()
        interval = 3.0

        try:
            current_start, current_end = current_round_key()

            with LOCK:
                round_history.cancel_unconfirmed_purchases(ARGS.asset, now_local())
                stored_start = STATE["round_start"]
                stored_end = STATE["round_end"]

            if stored_start is None or current_start != stored_start:
                if stored_end is not None:
                    settle_previous_round(stored_end)

                with LOCK:
                    ANALYSIS_TOKEN += 1
                    STATE["round_start"] = current_start
                    STATE["round_end"] = current_end
                    STATE["underlying_prices"] = deque(price_history.load(ARGS.asset,current_start,current_end),maxlen=2000)
                    STATE["target"] = None
                    STATE["prediction"] = None
                    STATE["live_recommendation"] = None
                    STATE["recommendation_minute"] = None
                    STATE["analysis_status"] = "waiting_target"
                    STATE["analysis_error"] = None
                    journal_round("waiting_target")

                start_history_refresh()

            price = SOURCE.latest()
            now = now_local()

            source_snapshot = getattr(SOURCE,'last_brti_snapshot',None) if SOURCE.mode == 'cfbenchmarks' else None
            source_time = source_snapshot['source_time'] if source_snapshot else now
            if source_snapshot:
                price = source_snapshot['value']
            with LOCK:
                if STATE['live_updated_at'] and source_time < STATE['live_updated_at']:
                    raise RuntimeError('Ignored a price update older than the displayed source time.')
                STATE["live_price"] = float(price)
                STATE["live_updated_at"] = source_time
                STATE["exchange_quotes"] = {"CF Benchmarks BRTI":float(price)} if source_snapshot else dict(SOURCE.last_quotes)
                STATE["exchange_errors"] = dict(SOURCE.last_errors)
                if (STATE.get('analysis_error') or '').startswith('Live price error:'):
                    STATE['analysis_error'] = None
                STATE["samples"].append((now, float(price)))
                record_underlying_price(current_start,current_end,source_time,float(price))
                round_end = STATE["round_end"]
                prediction = STATE["prediction"]
                history_index = HISTORY["index"]
                live_recommendation = STATE["live_recommendation"]

            minute_key = max(0, int((now - current_start).total_seconds() // 60))
            recommendation_due = (
                live_recommendation is None
                or (now - live_recommendation["updated_at"]).total_seconds()
                >= core.LIVE_RECOMMENDATION_REFRESH_SECONDS
            )
            if (
                prediction is not None
                and history_index is not None
                and recommendation_due
            ):
                recommendation = core.build_live_recommendation(
                    round_info=prediction,
                    current_price=float(price),
                    now=now,
                    index=history_index,
                    context_hours=ARGS.context_hours,
                )
                learner = ensure_live_learner(ARGS.asset, current_start, history_index)
                with LOCK:
                    underlying=list(STATE['underlying_prices'])
                    contract_samples=current_contract_price_history(current_start)
                recommendation=learner.apply(recommendation,underlying,current_start,round_end,core.confidence_strength)
                recommendation=live_market_support.apply(recommendation,underlying,contract_samples,current_start,round_end,core.confidence_strength)
                with LOCK:
                    if STATE["prediction"] is prediction:
                        price_history.append_live(ARGS.asset,current_start,recommendation)
                        try:
                            prospective_study.append(ARGS.asset,current_start,prospective_study.snapshot(
                                current_start,round_end,now,STATE['target'],underlying,
                                current_contract_prices(current_start),prediction,recommendation,dict(ORDER_SETTINGS)))
                        except Exception:
                            logging.getLogger(__name__).exception('Research snapshot could not be saved')
                        record_live_recommendation(recommendation)
                        STATE["live_recommendation"] = recommendation
                        STATE["recommendation_minute"] = minute_key
                        live_recommendation = recommendation

            seconds_left = (
                round_end - now
            ).total_seconds() if round_end else 999

            interval = core.SOURCE_POLL_INTERVAL_SECONDS if SOURCE.mode == 'cfbenchmarks' else (1.0 if seconds_left <= 65 else 3.0)
            if live_recommendation is not None:
                recommendation_age = (
                    now - live_recommendation["updated_at"]
                ).total_seconds()
                interval = min(
                    interval,
                    max(
                        0.15,
                        core.LIVE_RECOMMENDATION_REFRESH_SECONDS
                        - recommendation_age,
                    ),
                )

        except Exception as exc:
            with LOCK:
                if SOURCE.mode == 'cfbenchmarks':
                    STATE['live_recommendation'] = None
                    STATE['exchange_quotes'] = {}
                    STATE['exchange_errors'] = dict(SOURCE.last_errors)
                    # Rejected/failed fetches cannot replace the last accepted pair.
                    snapshot = getattr(SOURCE,'last_brti_snapshot',None)
                    if snapshot and (STATE['live_updated_at'] is None or snapshot['source_time'] > STATE['live_updated_at']):
                        STATE['live_price'] = snapshot['value']
                        STATE['live_updated_at'] = snapshot['source_time']
                if not STATE["analysis_error"] or STATE["analysis_error"].startswith('Live price error:'):
                    STATE["analysis_error"] = f"Live price error: {exc}"

        elapsed = time.time() - loop_start
        STOP_EVENT.wait(max(0.15, interval - elapsed))


@app.after_request
def compress_dashboard_response(response):
    if (request.path.startswith('/api/') and response.status_code==200
            and response.mimetype=='application/json' and not response.headers.get('Content-Encoding')):
        response.vary.add('Accept-Encoding')
        if request.accept_encodings['gzip']>0:
            payload=response.get_data()
            if len(payload)>=4096:
                compressed=gzip.compress(payload,compresslevel=1)
                if len(compressed)<len(payload):
                    response.set_data(compressed)
                    response.headers['Content-Encoding']='gzip'
    return response


@app.get("/api/order-status-logs")
def api_order_status_logs():
    try:
        before=int(request.args['before']) if request.args.get('before') else None
        if before is not None and not 1<=before<=9223372036854775807:
            raise ValueError
    except ValueError:
        return jsonify(error='Invalid log cursor.'),400
    try:
        result=order_logs.page(before)
        return jsonify(result)
    except (OSError,order_logs.sqlite3.Error):
        with LOCK:
            entries=list(reversed(STATE['order_status_logs']))
        return jsonify(logs=entries[:200],has_more=False,error='Archive unavailable; showing recent messages only.')


@app.get("/api/state")
def api_state():
    with LOCK:
        start = STATE["round_start"]
        end = STATE["round_end"]
        live = STATE["live_price"]
        target = STATE["target"]
        p = STATE["prediction"]
        now = now_local()

        seconds_left = (
            max(0, int((end - now).total_seconds()))
            if end else 0
        )

        source_stamp = STATE['live_updated_at']
        source_age = (now-source_stamp).total_seconds() if source_stamp else None
        quote_stale = source_age is None or not 0 <= source_age <= core.MAX_QUOTE_AGE_SECONDS
        last_observed_price = live
        if quote_stale:
            live = None
        live_delta = None
        live_delta_pct = None

        if live is not None and target:
            live_delta = live - target
            live_delta_pct = live_delta / target * 100.0

        current_trade_row = (round_history.get(ARGS.asset, dt_iso(start), include_quotes=False) or {}) if start else {}
        current_purchase = current_trade_row.get('purchase') or {}
        pending_close = current_purchase.get('close_order') or {}
        manual_close = ({'asset': ARGS.asset, 'round_start': dt_iso(start),
                         'purchase_token': current_purchase.get('attempted_at') or current_purchase.get('submitted_at') or '',
                         'close_token': pending_close.get('attempted_at') or pending_close.get('submitted_at') or ''}
                        if current_purchase.get('status') == 'filled' and pending_close.get('status') in ('attempted', 'submitted') else None)
        payload = {
            "manual_close": manual_close,
            "chart_trades": {"round_start": dt_iso(start), "round_end": dt_iso(end),
                            "prediction": current_trade_row.get("prediction") or (p or {}).get("prediction"),
                            "p_up": current_trade_row.get("p_up", (p or {}).get("p_up")),
                            "actual": current_trade_row.get("actual"), "status": current_trade_row.get("status"),
                            "purchase": current_purchase,
                            "completed_purchases": current_trade_row.get("completed_purchases") or []},
            "server_time": dt_iso(now),
            "asset": ARGS.asset,
            "supported_assets": sorted(core.ASSET_CONFIGS),
            "round": {
                "start": dt_iso(start),
                "end": dt_iso(end),
                "start_label": start.strftime("%H:%M") if start else None,
                "end_label": end.strftime("%H:%M") if end else None,
                "seconds_left": seconds_left,
            },
            "history": {
                "loading": HISTORY["loading"],
                "ready": HISTORY["index"] is not None,
                "error": HISTORY["error"],
                "message": STATE["history_message"],
                "updated_at": dt_iso(HISTORY["updated_at"]),
            },
            "analysis_status": STATE["analysis_status"],
            "analysis_error": STATE["analysis_error"],
            "target": money(target),
            "robinhood_status": STATE["robinhood_status"],
            "order_status_log_id": STATE["order_status_log_id"],
            "contract_prices": current_contract_prices(start),
            "contract_price_history": current_contract_price_history(start),
            "underlying_price_history": list(STATE["underlying_prices"]),
            "live_recommendation_changes": price_history.live_changes(ARGS.asset, start, end),
            "entry_stability": entry_stability.measure(STATE["underlying_prices"], start, end, now, target),
            "available_balance": current_available_balance(),
            "target_quality": STATE["target_quality"],
            "order_settings": dict(ORDER_SETTINGS),
            "force_buy_pending": bool(current_force_buy_request(ARGS.asset, start, end)),
            "live": {
                "price": money(live),
                "source": SOURCE.label,
                "transport":getattr(SOURCE,'last_transport',None),
                "fetched_at":dt_iso(getattr(SOURCE,'last_fetched_at',None)),
                "source_age_seconds":round(source_age,1) if source_age is not None else None,
                "stale":quote_stale,
                "last_observed_price":money(last_observed_price),
                "updated_at": dt_iso(STATE["live_updated_at"]),
                "delta": None if live_delta is None else round(live_delta, 2),
                "delta_pct": None if live_delta_pct is None else round(live_delta_pct, 4),
                "position": (
                    None if live_delta is None
                    else "ABOVE" if live_delta > 0
                    else "BELOW" if live_delta < 0
                    else "AT"
                ),
            },
            "quotes": {
                k: money(v)
                for k, v in STATE["exchange_quotes"].items()
            },
            "prediction": prediction_to_json(p),
            "live_recommendation": recommendation_to_json(
                STATE["live_recommendation"]
            ),
            "stats": dashboard_stats_json(),
            "latest_settlement": STATE["latest_settlement"],
            "warning": (
                f"Live source: {SOURCE.label}. Historical data uses an exchange proxy. "
                "The platform's official settlement can differ."
            ),
        }

    return jsonify(payload)


def open_purchase_pnl(row, now):
    purchase = row.get('purchase') or {}
    if (purchase.get('status') != 'filled' or row.get('actual') is not None
            or (purchase.get('close_order') or {}).get('status') == 'filled'
            or not datetime.fromisoformat(row['round_start']) <= now < datetime.fromisoformat(row['round_end'])):
        return None
    result = {'status':'unavailable','net_pnl':None}
    with LOCK:
        quote = dict(STATE.get('holding_quote') or {})
        if purchase.get('test_mode'):
            prices = dict(STATE.get('contract_prices') or {})
            quote = dict(prices, direction=purchase.get('direction'),
                         purchase_token=purchase.get('attempted_at') or purchase.get('submitted_at'),
                         bid_cents=prices.get('yes_cents' if purchase.get('direction') == 'UP / ABOVE' else 'no_cents'))
    if purchase.get('target') != row.get('target'):
        return result
    if (quote.get('asset') != row.get('asset') or quote.get('round_start') != row['round_start']
            or quote.get('target') != row.get('target') or quote.get('direction') != purchase.get('direction')
            or quote.get('purchase_token') != (purchase.get('attempted_at') or purchase.get('submitted_at'))):
        return result
    try:
        age = (now - datetime.fromisoformat(quote['updated_at'])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return result
    if not 0 <= age <= 10:
        return result
    pnl = round_history.trade_pnl(dict(purchase, close_order={'status':'filled',
          'contracts':purchase.get('contracts'),'execution_price_cents':quote.get('bid_cents')}), None)
    if pnl is None:
        return result
    return {'status':'winning' if pnl > 0 else 'losing' if pnl < 0 else 'breakeven',
            'net_pnl':pnl,'bid_cents':quote['bid_cents'],'updated_at':quote['updated_at'],
            'price_source':'displayed_contract_quote' if purchase.get('test_mode') else 'held_side_bid'}


@app.get("/api/round-history")
def api_round_history():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        return jsonify({"error": "Invalid page."}), 400
    with LOCK:
        asset = ARGS.asset
        rows = round_history.merged(asset, STATS.get("rounds", []), include_quotes=False)
        chop_source = ORDER_SETTINGS.get('choppiness_buy_guard', {}).get('source', 'bitcoin')
    try:
        date_from = datetime.strptime(request.args['date_from'], '%Y-%m-%d').date() if request.args.get('date_from') else None
        date_to = datetime.strptime(request.args['date_to'], '%Y-%m-%d').date() if request.args.get('date_to') else None
        if date_from and date_to and date_from > date_to:
            raise ValueError('Invalid range')
    except ValueError:
        return jsonify({'error': 'Enter valid dates with From on or before To.'}), 400
    rows = [row for row in rows if
            (date_from is None or datetime.fromisoformat(row['round_start']).date() >= date_from)
            and (date_to is None or datetime.fromisoformat(row['round_start']).date() <= date_to)]
    for row in rows:
        row["purchase_pnl"] = round_history.purchase_pnl(row)
        visible_purchase = row.get('purchase') or next(iter(reversed(row.get('completed_purchases') or [])), None)
        row['visible_purchase_pnl'] = round_history.trade_pnl(visible_purchase, row.get('actual')) if visible_purchase else None
        row['completed_purchase_pnls'] = [round_history.trade_pnl(trade, row.get('actual')) for trade in row.get('completed_purchases') or []]
        row["open_purchase_pnl"] = open_purchase_pnl(row, now_local())
        row["force_close_enabled"] = ORDER_SETTINGS.get("force_close", {}).get("enabled", False)
    purchases = request.args.get("purchases", "all")
    if purchases not in ("all", "only", "hide"):
        return jsonify({"error": "Invalid purchase filter."}), 400
    if purchases != "all":
        rows = [row for row in rows
                if any(trade.get('status') == 'filled' for trade in round_history.round_trades(row)) == (purchases == "only")]
    column_keys = {"final": None, "pattern": "Similar-pattern", "recent": "Recent 3h regime",
                   "bootstrap": "Historical bootstrap", "trend": "Trend/regime", "live": None, "actual": None}
    for column, model in column_keys.items():
        choice = request.args.get(column, "all")
        allowed = ("all", "up", "down") if column == "actual" else ("all", "up", "down", "hit", "miss")
        if choice not in allowed:
            return jsonify({"error": "Invalid column filter."}), 400
        if choice == "all":
            continue
        def matches(row):
            if model:
                item = (row.get("component_results") or {}).get(model) or {}
                direction, hit = item.get("predicted"), item.get("hit")
            elif column == "live":
                item = (row.get("purchase") or {}).get("live_recommendation") or {}
                direction = item.get("direction")
                hit = direction == row['actual'] if direction and row.get('actual') else None
            elif column == "actual":
                direction, hit = row.get("actual"), None
            else:
                direction, hit = row.get("prediction"), row.get("hit")
            if choice in ("up", "down"):
                return bool(direction and direction.startswith("UP" if choice == "up" else "DOWN"))
            return hit is (choice == "hit")
        rows = [row for row in rows if matches(row)]
    cutoff = now_local()
    completed_rows = [row for row in rows if datetime.fromisoformat(row['round_end']) <= cutoff]
    filtered_stats = round_history.purchase_accuracy(completed_rows)
    filtered_stats["final_prediction"] = round_history.final_prediction_accuracy(completed_rows)
    filtered_stats["balance"] = round_history.balance_summary(completed_rows, 0)
    size = 25
    pages = max(1, math.ceil(len(rows) / size))
    page = min(page, pages)
    page_rows=rows[(page-1)*size:page*size]
    for row in page_rows:
        stored=round_history.get(asset,row['round_start']) or {}
        row['contract_price_history']=stored.get('contract_price_history',row.get('contract_price_history',[]))
        row['underlying_price_history']=price_history.load(asset,datetime.fromisoformat(row['round_start']),datetime.fromisoformat(row['round_end']))
        row['live_recommendation_changes']=price_history.live_changes(asset,datetime.fromisoformat(row['round_start']),datetime.fromisoformat(row['round_end']))
        row['choppiness'] = round_choppiness(
            asset, datetime.fromisoformat(row['round_start']), datetime.fromisoformat(row['round_end']), cutoff, chop_source)
    return jsonify({"asset": asset, "server_time":dt_iso(cutoff), "page": page, "pages": pages, "total": len(rows),
                    "stats": filtered_stats, "rounds": page_rows})


@app.post("/api/purchase-filled")
def api_purchase_filled():
    data = request.get_json(silent=True) or {}
    with LOCK:
        if data.get("asset") != ARGS.asset:
            return jsonify({"ok": False, "error": "Selected asset changed."}), 409
        rows = round_history.merged(ARGS.asset, STATS.get("rounds", []), include_quotes=False)
        row = next((r for r in rows if r["round_start"] == data.get("round_start")), None)
        if not row or row.get("prediction") not in ("UP / ABOVE", "DOWN / BELOW"):
            return jsonify({"ok": False, "error": "No recorded prediction for this round."}), 400
        previous = row.get("purchase") or row.get("removed_purchase") or {"direction": row["prediction"], "target": row.get("target")}
        purchase = dict(previous, status="filled", confirmed_by="user",
                        confirmed_at=dt_iso(now_local()))
        fields = {key: value for key, value in row.items() if key not in ("asset", "round_start", "round_end", "purchase", "purchase_removed", "removed_purchase")}
        round_history.upsert(ARGS.asset, row["round_start"], row["round_end"], purchase=purchase, purchase_removed=False, removed_purchase=None, **fields)
    return jsonify({"ok": True})


@app.post("/api/purchase-closed")
def api_purchase_closed():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error='Invalid request.'), 400
    with LOCK:
        if data.get('asset') != ARGS.asset:
            return jsonify(ok=False, error='Selected asset changed.'), 409
        row = round_history.get(ARGS.asset, data.get('round_start')) or {}
        purchase = row.get('purchase') or {}
        closing = purchase.get('close_order') or {}
        if (purchase.get('status') != 'filled' or not closing
                or data.get('purchase_token') != (purchase.get('attempted_at') or purchase.get('submitted_at') or '')
                or data.get('close_token') != (closing.get('attempted_at') or closing.get('submitted_at') or '')):
            return jsonify(ok=False, error='The trade or close order changed. Refresh and try again.'), 409
        if closing.get('status') == 'filled':
            return jsonify(ok=True)
        if closing.get('status') not in ('attempted', 'submitted'):
            return jsonify(ok=False, error='No pending close order to confirm.'), 409
        price = data.get('price_cents')
        if (not isinstance(price, (int, float)) or isinstance(price, bool)
                or not math.isfinite(price) or not 0 <= price <= 100):
            return jsonify(ok=False, error='Closing price must be between 0 and 100 cents.'), 400
        stamp = dt_iso(now_local())
        closing = dict(closing, execution_price_cents=float(price), status='filled', confirmed_by='user', confirmed_at=stamp,
                       filled_at=stamp, filled_quantity=purchase.get('contracts'))
        round_history.upsert(ARGS.asset, row['round_start'], row['round_end'],
                             purchase=dict(purchase, close_order=closing))
        robinhood_report('Close filled: manually confirmed closed by user.')
    return jsonify(ok=True)


@app.post("/api/purchase-remove")
def api_purchase_remove():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Invalid purchase record."}), 400
    with LOCK:
        if data.get("asset") != ARGS.asset:
            return jsonify({"ok": False, "error": "Selected asset changed."}), 409
        rows = round_history.merged(ARGS.asset, STATS.get("rounds", []), include_quotes=False)
        row = next((r for r in rows if r["round_start"] == data.get("round_start")), None)
        if not row or not row.get("purchase"):
            return jsonify({"ok": False, "error": "No purchase record for this round."}), 404
        fields = {key: value for key, value in row.items()
                  if key not in ("asset", "round_start", "round_end", "purchase", "purchase_removed", "removed_purchase", "purchase_removal_revision")}
        round_history.upsert(ARGS.asset, row["round_start"], row["round_end"],
                             purchase=None, purchase_removed=True,
                             purchase_removal_revision=row.get('purchase_removal_revision', 0) + 1,
                             removed_purchase=row["purchase"], **fields)
    return jsonify({"ok": True})


@app.post("/api/purchase-price")
def api_purchase_price():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Invalid purchase record."}), 400
    value = data.get("price_cents")
    if isinstance(value, bool):
        return jsonify({"ok": False, "error": "CT price must be above 0 and at most 100 cents."}), 400
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return jsonify({"ok": False, "error": "Enter a numeric CT price in cents."}), 400
    if not math.isfinite(price) or not 0 < price <= 100:
        return jsonify({"ok": False, "error": "CT price must be above 0 and at most 100 cents."}), 400
    with LOCK:
        if data.get("asset") != ARGS.asset:
            return jsonify({"ok": False, "error": "Selected asset changed."}), 409
        rows = round_history.merged(ARGS.asset, STATS.get("rounds", []), include_quotes=False)
        row = next((r for r in rows if r["round_start"] == data.get("round_start")), None)
        if not row or not row.get("purchase"):
            return jsonify({"ok": False, "error": "No purchase record for this round."}), 404
        purchase = dict(row["purchase"])
        purchase.setdefault("original_limit_price_cents", purchase.get("limit_price_cents"))
        purchase.update(limit_price_cents=price, price_edited_at=dt_iso(now_local()))
        fields = {key: value for key, value in row.items()
                  if key not in ("asset", "round_start", "round_end", "purchase")}
        round_history.upsert(ARGS.asset, row["round_start"], row["round_end"], purchase=purchase, **fields)
    return jsonify({"ok": True, "price_cents": price})


@app.post("/api/trade-close-overrides")
def api_trade_close_overrides():
    data = request.get_json(silent=True)
    allowed = ('ignore_early_profit', 'ignore_stop_loss')
    if (not isinstance(data, dict) or data.get('field') not in allowed
            or not isinstance(data.get('value'), bool)):
        return jsonify(ok=False, error='Invalid trade close override.'), 400
    with LOCK:
        start, end = current_round_key()
        if (data.get('asset') != ARGS.asset or data.get('round_start') != dt_iso(start)
                or not start <= now_local() < end):
            return jsonify(ok=False, error='Only the current trade can be changed.'), 409
        row = (round_history.get(ARGS.asset,dt_iso(start)) or {})
        purchase = dict(row.get('purchase') or {})
        token = purchase.get('attempted_at') or purchase.get('submitted_at')
        if (not token or data.get('purchase_token') != token or purchase.get('status') != 'filled'
                or purchase.get('close_order')):
            return jsonify(ok=False, error='The trade changed or already has a close order.'), 409
        purchase[data['field']] = data['value']
        round_history.upsert(ARGS.asset,dt_iso(start),dt_iso(end),purchase=purchase)
    return jsonify(ok=True)


@app.post("/api/force-buy")
def api_force_buy():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error='Invalid request.'), 400
    with LOCK:
        start, end = current_round_key()
        rule = ORDER_SETTINGS.get('force_buy', {})
        if not rule.get('enabled'):
            return jsonify(ok=False, error='Enable Force Buy and save settings first.'), 409
        if (data.get('asset') != ARGS.asset or data.get('round_start') != dt_iso(start)
                or STATE.get('round_start') != start or STATE.get('target') is None):
            return jsonify(ok=False, error='Current round or target changed.'), 409
        if not start <= now_local() < end or (now_local()-start).total_seconds()/60 >= rule['minute_to']:
            return jsonify(ok=False, error='Force Buy window has ended.'), 409
        row = (round_history.get(ARGS.asset,dt_iso(start)) or {})
        purchase = row.get('purchase') or {}
        if purchase and (not ORDER_SETTINGS.get('allow_multi_buy', True)
                         or (purchase.get('close_order') or {}).get('status') != 'filled'):
            return jsonify(ok=False, error='An open position or purchase attempt already exists.'), 409
        if not current_force_buy_request(ARGS.asset, start, end):
            STATE['force_buy_request'] = {'asset':ARGS.asset, 'round_start':dt_iso(start),
                                          'analysis_token':ANALYSIS_TOKEN, 'requested_at':dt_iso(now_local())}
            robinhood_report('Force Buy requested: waiting for the configured conditions and fresh Live direction.')
    return jsonify(ok=True)


@app.post("/api/force-close")
def api_force_close():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error='Invalid request.'), 400
    with LOCK:
        start, end = current_round_key()
        if data.get('asset') != ARGS.asset or data.get('round_start') != dt_iso(start):
            return jsonify(ok=False, error='Force Close is available only for the current round.'), 409
        if not ORDER_SETTINGS.get('force_close', {}).get('enabled', False):
            return jsonify(ok=False, error='Enable Force Close and save settings before pressing its button.'), 409
        row = (round_history.get(ARGS.asset,dt_iso(start)) or None)
        purchase = dict((row or {}).get('purchase') or {})
        token = purchase.get('attempted_at') or purchase.get('submitted_at')
        if (purchase.get('status') != 'filled' or purchase.get('close_order')
                or not token or data.get('purchase_token') != token
                or purchase.get('target') != STATE['target']):
            return jsonify(ok=False, error='The open position changed or already has a close order.'), 409
        if not purchase.get('force_close_token'):
            purchase.update(force_close_token=token, force_close_requested_at=dt_iso(now_local()))
            round_history.upsert(ARGS.asset, dt_iso(start), dt_iso(end), purchase=purchase)
            robinhood_report('Force close requested: waiting for the held-side bid and verified close review.')
    return jsonify(ok=True)


@app.post("/api/order-settings")
def api_order_settings():
    try:
        data = request.get_json(silent=True)
        if isinstance(data, dict) and 'initial_balance' not in data:
            with LOCK:
                data = dict(data, initial_balance=ORDER_SETTINGS.get('initial_balance', 0))
        if isinstance(data, dict) and 'test_mode' not in data:
            with LOCK:
                data = dict(data, test_mode=ORDER_SETTINGS.get('test_mode', False))
        if isinstance(data, dict) and isinstance(data.get('choppiness_buy_guard'), dict):
            with LOCK:
                # Older open pages must not erase newly configured gates.
                existing = ORDER_SETTINGS.get('choppiness_buy_guard', {})
                additions = {key: existing.get(key, order_settings.CHOPPINESS_BUY_GUARD_DEFAULTS[key])
                             for key in ('current_enabled', 'current_limit', 'block_next_round')
                             if key not in data['choppiness_buy_guard']}
                data = dict(data, choppiness_buy_guard=dict(data['choppiness_buy_guard'], **additions))
        if isinstance(data, dict):
            with LOCK:
                data = dict(data, **{key: data.get(key, ORDER_SETTINGS.get(key, order_settings.DEFAULTS[key])) for key in ('require_final_live_agreement', 'min_model_agreement')})
        settings = order_settings.validate(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    with LOCK:
        try:
            order_settings.save(settings)
        except OSError:
            return jsonify({"ok": False, "error": "Could not save order settings."}), 500
        if settings.get('force_buy') != ORDER_SETTINGS.get('force_buy'):
            STATE['force_buy_request'] = None
        ORDER_SETTINGS.update(settings)
    return jsonify({"ok": True, "order_settings": settings})


BUY_ANALYSIS_CACHE = {}
BUY_ANALYSIS_LOCK = threading.Lock()


@app.get('/api/buy-analysis')
def api_buy_analysis():
    with LOCK:
        asset = ARGS.asset
    with BUY_ANALYSIS_LOCK:
        cached = BUY_ANALYSIS_CACHE.get(asset)
        if cached and time.monotonic() - cached[0] < 60 and request.args.get('refresh') != '1':
            return jsonify(dict(cached[1], asset=asset))
        with LOCK:
            records = round_history.merged(asset, STATS.get('rounds', []) if ARGS.asset == asset else [])
        result = buy_analysis.analyze(records, now_local())
        BUY_ANALYSIS_CACHE[asset] = (time.monotonic(), result)
    return jsonify(dict(result, asset=asset))


@app.post("/api/balance-settings")
def api_balance_settings():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'initial_balance' not in data:
        return jsonify({"ok": False, "error": "Enter a starting balance."}), 400
    with LOCK:
        try:
            settings = order_settings.validate(dict(ORDER_SETTINGS, initial_balance=data['initial_balance']))
            order_settings.save(settings)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except OSError:
            return jsonify({"ok": False, "error": "Could not save balance."}), 500
        ORDER_SETTINGS.update(settings)
    return jsonify({"ok": True, "initial_balance": settings['initial_balance']})


@app.post("/api/target")
def api_target():
    global ANALYSIS_TOKEN

    data = request.get_json(silent=True) or {}

    try:
        target = float(data.get("target"))
    except Exception:
        return jsonify({"ok": False, "error": "Enter a valid numeric target."}), 400

    if not math.isfinite(target) or target <= 0:
        return jsonify({"ok": False, "error": "Target must be greater than 0."}), 400

    start, end = current_round_key()

    with LOCK:
        ANALYSIS_TOKEN += 1
        token = ANALYSIS_TOKEN
        STATE["round_start"] = start
        STATE["round_end"] = end
        STATE["target"] = target
        STATE["target_quality"] = "MANUAL Robinhood target"
        STATE["prediction"] = None
        STATE["live_recommendation"] = None
        STATE["recommendation_minute"] = None
        STATE["analysis_status"] = "analyzing"
        STATE["analysis_error"] = None
        journal_round("analyzing", target=target, target_quality="MANUAL Robinhood target")

    threading.Thread(
        target=analyze_target,
        args=(target, start, end, token),
        daemon=True,
    ).start()

    return jsonify({"ok": True, "status": "analyzing"}), 202


@app.post("/api/reload-history")
def api_reload_history():
    start_history_refresh()
    return jsonify({"ok": True, "status": "refreshing"})


@app.post("/api/asset")
def api_asset():
    global ANALYSIS_TOKEN, SOURCE

    data = request.get_json(silent=True) or {}
    asset = str(data.get("asset", "")).upper()
    if asset not in core.ASSET_CONFIGS:
        return jsonify({"ok": False, "error": "Unsupported asset."}), 400

    with LOCK:
        ANALYSIS_TOKEN += 1
        ARGS.asset = asset
        SOURCE = core.BenchmarkSource("auto", asset)
        STATS.clear()
        STATS.update(core.load_stats(asset))
        HISTORY.update({"candles": None, "index": None, "records": None,
                        "updated_at": None, "loading": False, "error": None})
        start, end = current_round_key()
        STATE.update({
            "round_start": start, "round_end": end, "target": None,
            "prediction": None, "live_recommendation": None,
            "recommendation_minute": None, "analysis_status": "waiting_target",
            "analysis_error": None, "live_price": None, "live_updated_at": None,
            "exchange_quotes": {}, "exchange_errors": {},
            "samples": deque(maxlen=180), "underlying_prices":deque(price_history.load(asset,start,end),maxlen=2000), "latest_settlement": None,
            "history_message": f"Loading {asset} historical data...",
        })

        journal_round("waiting_target")

    threading.Thread(target=refresh_history, kwargs={"force": True}, daemon=True).start()
    return jsonify({"ok": True, "asset": asset, "status": "refreshing"}), 202


@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


@app.get("/dashboard.css")
def dashboard_styles():
    return Response(HTML.split('<style>', 1)[1].split('</style>', 1)[0], mimetype="text/css")


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC TargetPulse</title>
<style>
:root{
  --bg:#080c12;--panel:#101720;--panel2:#0c121a;--line:#223041;
  --text:#edf3fb;--muted:#8ea0b5;--green:#30d27c;--red:#ff5c6c;
  --yellow:#f5c451;--cyan:#43c9ff;--blue:#669cff
}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:#52667e #101720}
:root{color-scheme:dark}
@supports selector(::-webkit-scrollbar){
  *{scrollbar-width:auto;scrollbar-color:auto}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-track{background:var(--panel);border-radius:999px}
  ::-webkit-scrollbar-thumb{background:#52667e;border:2px solid var(--panel);border-radius:999px;min-height:28px}
  ::-webkit-scrollbar-thumb:hover{background:#7894b3}
  ::-webkit-scrollbar-thumb:active{background:var(--cyan)}
  ::-webkit-scrollbar-corner{background:var(--panel)}
  ::-webkit-scrollbar-button{display:none;width:0;height:0}
}
body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}
.wrap{max-width:2200px;margin:0 auto;padding:20px}
.header{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:16px}
.title{font-size:24px;font-weight:800}
.subtitle{font-size:13px;color:var(--muted);margin-top:4px}
.header-text-row{display:flex;align-items:center;justify-content:space-between;gap:24px;flex:1;min-width:0;overflow-x:auto;white-space:nowrap}
.header-text-row>.title,.header-text-row>.subtitle{flex-shrink:0}
.header-text-row>.subtitle{margin-top:0;text-align:right}
@media(max-width:850px){.header-text-row{width:100%;flex:auto}}
.badge{border:1px solid var(--line);border-radius:999px;padding:8px 12px;color:var(--yellow);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:14px;padding:16px}
.span4{grid-column:span 4}.span5{grid-column:span 5}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}
.label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}
.big{font-size:30px;font-weight:800;margin-top:5px}.medium{font-size:20px;font-weight:750;margin-top:5px}
.muted{color:var(--muted)}.green{color:var(--green)}.red{color:var(--red)}.yellow{color:var(--yellow)}.cyan{color:var(--cyan)}
[hidden]{display:none!important}
.row{display:flex;gap:18px;align-items:center;flex-wrap:wrap}.spread{display:flex;justify-content:space-between;gap:12px;align-items:center}
input,select{width:220px;background:#080d14;color:white;border:1px solid #34455a;padding:12px;border-radius:10px;font-size:18px;outline:none}
input:focus,select:focus{border-color:var(--cyan)}
button{padding:12px 16px;border:0;border-radius:10px;font-weight:750;cursor:pointer;background:var(--cyan);color:#00141f}
button.secondary{background:#1b2735;color:var(--text);border:1px solid var(--line)}
button.confirm-closed,button#confirmClosedStatus{padding:2px 6px;font-size:11px;line-height:16px;border-radius:4px;font-weight:500;white-space:nowrap}
button:disabled{opacity:.45;cursor:not-allowed}
.direction{padding:20px;border-radius:12px;border:1px solid var(--line);text-align:center}
.direction.up{box-shadow:inset 0 0 0 1px rgba(48,210,124,.25)}
.direction.down{box-shadow:inset 0 0 0 1px rgba(255,92,108,.25)}
.direction .decision{font-size:34px;font-weight:900}
.probbar{height:10px;background:#1b2735;border-radius:999px;overflow:hidden;margin-top:12px}
.probbar>div{height:100%;background:var(--green)}
.models{width:100%;border-collapse:collapse;margin-top:8px}
.models th,.models td{border-bottom:1px solid var(--line);padding:10px 6px;text-align:right;font-size:14px}
.models th:first-child,.models td:first-child{text-align:left}.models th{color:var(--muted);font-weight:600}
.quote{display:inline-block;padding:6px 9px;background:#0a1119;border:1px solid var(--line);border-radius:8px;margin:3px;font-size:13px}
.status{padding:10px 12px;border-radius:10px;background:#0a1119;border:1px solid var(--line);color:var(--muted);font-size:13px}
.warning{color:var(--yellow);font-size:12px}.sep{height:1px;background:var(--line);margin:12px 0}
@media(max-width:850px){.span4,.span5,.span7,.span8{grid-column:span 12}.header{align-items:flex-start;flex-direction:column}input{width:100%}.target-form{width:100%}.target-form button{width:100%;margin-top:8px}}
.round-log-scroll{overflow:visible;margin-top:14px;border:1px solid var(--line);border-radius:10px}
.round-log{width:100%;min-width:0;border-collapse:separate;border-spacing:0;table-layout:fixed}
.round-log th{position:sticky;top:0;background:#182432;color:#cbd8e8;z-index:1;padding:14px 10px;text-align:center;font-size:13px;border-bottom:1px solid #3a4b61}
.round-log th{width:10%;box-sizing:border-box}.round-log th:first-child{text-align:left}
.round-log td{padding:18px 10px;text-align:center;vertical-align:middle;font-size:14px;line-height:1.6;border-bottom:1px solid #2b3a4d}
.round-log tbody tr:nth-child(odd){background:#101a27}.round-log tbody tr:nth-child(even){background:#142031}.round-log tbody tr:hover{background:#203047}
.round-log td:first-child{text-align:left}.round-log td:nth-child(3){background:rgba(67,201,255,.04)}
#roundHistoryBody tr,#roundHistoryBody tr>td{background:transparent}
#roundHistoryBody tr.round-group-start>td{border-top:1px solid var(--line)}
#roundHistoryBody tr.round-frame>td:first-child{border-left:1px solid #45566c}
#roundHistoryBody tr.round-frame>td:last-child{border-right:1px solid #45566c}
#roundHistoryBody tr.round-group-start>td{border-top:1px solid #45566c}
#roundHistoryBody tr.round-group-end>td{border-bottom:1px solid #45566c}
#roundHistoryBody tr.round-gap>td{height:5px;padding:0;border:0;line-height:0;font-size:0}
.round-current-label{display:inline-block;color:var(--cyan);border:1px solid var(--cyan);border-radius:4px;padding:1px 5px;margin:2px 0;font-size:10px;font-weight:700;line-height:1.4}
.round-tag{display:inline-block;font-size:12px;font-weight:750;padding:3px 9px;border:1px solid currentColor;border-radius:6px;margin:3px 0}
.round-prob{display:block;font-size:12px;color:#bbc9da;margin-top:4px}.round-time{font-weight:750;font-size:15px;color:#edf3fb;white-space:normal}.round-date{color:#aebed1;font-size:12px}
.history-column-filter{display:block;width:100%;min-width:0;margin-top:5px;padding:3px 2px;font-size:11px}
.history-header-row{display:grid;grid-template-columns:1fr auto;align-items:center;gap:12px;margin-bottom:10px;white-space:nowrap;overflow-x:auto}
.history-filter-toolbar{display:flex;gap:10px;align-items:center;flex-wrap:nowrap;overflow-x:auto;margin-bottom:10px}
.history-filter-toolbar input,.history-filter-toolbar select,.history-filter-toolbar button{height:32px;box-sizing:border-box;padding:6px 8px;font-size:12px;border-radius:8px;flex-shrink:0}
.history-filter-toolbar label{flex-shrink:0}
.history-purchase-filter{display:flex;align-items:center;justify-content:center;gap:8px;font-size:12px}
.history-header-row #roundHistoryCount{justify-self:end}
.round-log .edit-purchase-price{padding:0 3px;font-size:12px;line-height:16px}
.round-log .remove-purchase{margin-left:6px;padding:0 5px;font-size:16px;line-height:20px;color:var(--red)}
.round-status{display:inline-block;font-size:12px;font-weight:400;vertical-align:middle}
.round-empty{color:#aebed1;font-size:12px}.round-log details{font-size:12px;max-width:175px;overflow-wrap:anywhere}
.contract-settings{display:flex;align-items:center;gap:18px;flex-wrap:nowrap}
.contract-settings>.label{white-space:nowrap}.contract-field{display:flex;align-items:center;gap:8px;white-space:nowrap}
.contract-settings input{width:80px;min-width:0;padding:9px;font-size:16px;box-sizing:border-box}.contract-settings button{white-space:nowrap}
.contract-settings #orderSettingsStatus{font-size:12px;min-width:0;overflow-wrap:anywhere}
.round-log th,.round-log td{overflow-wrap:anywhere;padding-left:7px;padding-right:7px}.round-log .confirm-filled{padding:6px;font-size:11px;white-space:normal}
@media(max-width:850px){.contract-settings{gap:10px}.contract-settings>.label{display:none}.contract-settings input{width:64px}.contract-field{gap:5px;font-size:13px}.contract-settings button{padding:10px;font-size:12px}.round-log th,.round-log td{font-size:11px;padding:12px 3px}.round-log .round-time{font-size:12px}.round-log .round-tag{padding:2px 4px;font-size:10px}.round-log .round-prob{font-size:10px}}
@media(max-width:450px){.contract-settings{gap:6px}.contract-settings input{width:48px;padding:7px 3px}.contract-settings button{font-size:11px;padding:9px 6px}.contract-field{font-size:11px}.round-log th,.round-log td{font-size:9px}.round-log .round-prob,.round-log .round-date{font-size:9px}}
.monitor-layout{display:grid;grid-template-columns:690px minmax(0,1fr);gap:18px;align-items:start}
.monitor-info,.monitor-history{min-width:0}
.monitor-info .card{padding:14px}.monitor-info .big{font-size:26px}.monitor-info .direction{padding:14px 8px}.monitor-info .direction .decision{font-size:26px}
.monitor-info .card:has(.models),.monitor-info .card:has(#btAccuracy){grid-column:span 12}
.monitor-info .contract-settings{gap:10px}.monitor-info .contract-settings>.label{display:none}.monitor-info .contract-field{font-size:12px;gap:5px}.monitor-info .contract-settings input{width:58px}.monitor-info .contract-settings button{padding:10px}
.monitor-info .target-form{display:flex;gap:8px;align-items:center}.monitor-info #targetInput{width:145px;min-width:0}.monitor-info #targetBtn{padding:12px 10px;font-size:12px}
.monitor-info .spread{flex-wrap:wrap}.monitor-history .round-log th,.monitor-history .round-log td{padding:10px 6px;font-size:12px}.monitor-history .round-time{font-size:13px}.monitor-history .round-prob{font-size:11px}
@media(max-width:1150px){.monitor-layout{grid-template-columns:minmax(0,1fr)}}
@media(max-width:650px){.monitor-history .round-log th,.monitor-history .round-log td{font-size:9px}.monitor-history .round-prob{font-size:9px}}
/* Compact current-round information without changing history table sizing. */
.accuracy-comparison{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.accuracy-comparison>div{min-width:0}
.price-target-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;min-width:0}
.price-target-row>.card{min-width:0}
.monitor-info{gap:8px;font-size:12px;line-height:1.35}
.monitor-info .card{padding:9px 10px;border-radius:10px}
.monitor-info .label{font-size:10px;letter-spacing:.5px}
.monitor-info .big{font-size:22px;line-height:1.2;margin-top:3px}
.monitor-info .medium{font-size:14px}
.monitor-info .direction{padding:8px 5px;margin-top:4px}
.monitor-info .direction .decision{font-size:20px;line-height:1.2}
.monitor-info .probbar{height:6px;margin-top:6px}
.monitor-info .status{padding:5px 7px;font-size:11px;border-radius:6px}
.monitor-info #analysisStatus{margin-top:6px!important}
.monitor-info .models{margin-top:4px}
.monitor-info .models th,.monitor-info .models td{padding:5px 3px;font-size:11px}
.monitor-info .sep{margin:6px 0}
.monitor-info .quote{padding:3px 5px;margin:2px;font-size:11px}
.monitor-info input{padding:7px 8px;font-size:13px}
.monitor-info .contract-settings{gap:5px}.monitor-info #initialBalanceInput{width:62px}.monitor-info .contract-field{font-size:11px}
.monitor-info .contract-settings input{width:52px;padding:6px}
.monitor-info #timeInForceInput,.monitor-info #closeTimeInForceInput,.monitor-info #minLiveStrengthInput,.monitor-info #stopLossTimeInForceInput,.monitor-info #forceCloseTimeInForceInput,.monitor-info #forceBuyTimeInForceInput{width:190px;max-width:100%;height:30px;padding:4px 6px;font-size:12px;border-radius:8px}
.monitor-info button,.monitor-info #targetBtn,.monitor-info .contract-settings button{padding:7px 9px;font-size:11px}
.contract-settings{flex-wrap:wrap}
.purchase-conditions{flex-basis:100%}
.order-save-row{flex-basis:100%;display:flex;justify-content:flex-end}
.current-balance-highlight{color:var(--cyan);background:#43c9ff12;border:1px solid #43c9ff55;border-radius:8px;padding:4px 10px}
.current-balance-highlight #currentBalance{font-size:16px;font-weight:800}
.balance-summary{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:2px 0}
.monitor-info .balance-summary #initialBalanceInput{width:90px}
.monitor-info .balance-summary input,.monitor-info .balance-summary button,.balance-summary .current-balance-highlight,.balance-summary .balance-pnl{height:32px;box-sizing:border-box}
.balance-summary .current-balance-highlight,.balance-summary .balance-pnl{display:inline-flex;align-items:center;gap:5px}
.balance-compact{display:flex;align-items:center;gap:14px;font-size:13px}
.balance-compact[hidden]{display:none}
.monitor-info button.section-toggle{display:inline-flex;align-items:center;gap:6px;background:transparent;border:0;border-radius:4px;padding:2px 0;color:var(--muted);font-weight:400;text-align:left}
.section-arrow{font-size:13px;width:10px}
.section-toggle:focus-visible{outline:2px solid var(--blue);outline-offset:4px}
#mergedQuotes #contractPrices{position:static;transform:none;width:100%;justify-content:center;flex-wrap:wrap;margin:2px 0}
.contract-chart-frame{position:relative}
.contract-chart-frame:has(#contractPriceHistory[hidden]) #contractPrices{position:static;margin-top:8px;flex-wrap:wrap;white-space:normal;transform:none;justify-content:center;width:100%}
#contractPriceHistory[hidden],#modelsContent[hidden],#balanceContent[hidden]{display:none}
#contractPrices{position:absolute;left:50%;transform:translateX(-50%);top:40px;z-index:1;display:flex;align-items:center;gap:16px;white-space:nowrap;font-size:16px;font-weight:700;pointer-events:none}
@media(max-width:650px){#contractPrices{gap:10px;font-size:14px;top:38px}}
#contractPrices>span{flex-shrink:0}
#contractPrices>span:first-child{color:var(--green)}
#contractPrices>span:nth-child(2){color:var(--red)}
#contractPriceTime{color:var(--cyan);font-size:.85em;font-weight:500}
.round-log td:has(>.round-purchase-details){padding:5px 6px;line-height:14px}
.round-purchase-details summary{line-height:14px;padding:0;margin:0}
.round-purchase-details{text-align:left;padding:0}
.round-timing-fields{display:flex;align-items:center;flex-wrap:wrap;gap:4px 16px}
.round-timing-fields .round-prob{display:inline;margin:0}
.round-chart-button{background:none;border:0;padding:0;color:var(--cyan);font-size:11px;line-height:14px;text-decoration:underline;cursor:pointer}
#roundChartDialog{width:min(1000px,92vw);max-width:92vw;max-height:85vh;overflow:auto;background:var(--card,#161e28);color:var(--text,#edf3fb);border:1px solid var(--line);border-radius:10px;padding:14px}
#roundChartDialog::backdrop{background:rgba(0,0,0,.7)}
.round-purchase-details .order-conditions{margin:0;max-width:none}
.round-purchase-details .order-conditions[open]>summary{margin-bottom:3px}
.purchase-detail-fields{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin-top:3px}
.purchase-detail-fields>div,.purchase-detail-fields>span{padding:2px 6px;border:1px solid var(--line);border-radius:5px;line-height:1.4}
.closed-trade-fields{margin:0;padding:5px 0}
.round-purchase-details details.closed-trade-fields{max-width:none;width:100%}
.trade-line{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 8px;min-width:0;list-style:none;cursor:pointer}
.trade-line::-webkit-details-marker{display:none}
.trade-line>.closed-trade-heading{flex-shrink:0}
.trade-conditions-toggle{font-size:11px;color:var(--muted);white-space:nowrap}
.trade-conditions-toggle::before{content:'▸ ';}
.closed-trade-fields[open]>.trade-line .trade-conditions-toggle::before{content:'▾ ';}
.trade-line .closed-trade-execution{width:auto;max-width:100%;min-width:0;flex:1 1 500px;flex-wrap:wrap;margin:0}
.trade-line .closed-trade-execution>span{white-space:nowrap;margin-top:0}
#forceBuyBtn{padding:3px 9px;font-size:11px;line-height:18px;border-radius:5px;white-space:nowrap}
.monitor-info .force-buy-inputs{flex-wrap:wrap;overflow:visible;min-width:0;width:100%;box-sizing:border-box}
.trade-close-toggle{display:inline-flex;align-items:center;gap:4px;white-space:nowrap;font-size:11px;color:var(--muted);cursor:pointer}
.monitor-history .trade-close-toggle input{width:12px;height:12px;margin:0;padding:0;accent-color:var(--cyan)}
.trade-line .force-close{padding:2px 7px;font-size:11px;line-height:16px;min-height:0;border-radius:5px;white-space:nowrap;align-self:center}
.trade-conditions-content{margin-top:5px}
.trade-conditions-content .purchase-detail-fields>div{min-width:0;max-width:100%;box-sizing:border-box;overflow-wrap:anywhere}
.closed-trade-heading{flex-shrink:0}
.trade-line>.order-conditions{min-width:0;max-width:100%;margin:0}
.trade-line>.order-conditions[open]{max-width:100%;white-space:normal}
.trade-line>.order-conditions .purchase-detail-fields{flex-wrap:wrap;white-space:normal}
.trade-line>.order-conditions .purchase-detail-fields>div{min-width:0;max-width:100%;box-sizing:border-box;overflow-wrap:anywhere}
.trade-line>.order-conditions summary{white-space:nowrap}
.trade-line .closed-trade-execution{width:auto;max-width:100%;min-width:0;flex:1 1 500px;flex-wrap:wrap;margin:0}
.trade-line .closed-trade-execution>span{white-space:nowrap;margin-top:0}
.closed-trade-heading{font-size:12px;font-weight:600}
.closed-trade-execution{display:flex;flex-wrap:wrap;width:100%;box-sizing:border-box;margin-top:3px;padding:0;border:0}
.closed-trade-fields+.closed-trade-fields{margin-top:4px;border-top:1px solid var(--line)}
.purchase-detail-fields>.round-tag{border-color:currentColor}
.contract-chart{width:100%;min-width:220px;margin-top:6px}
.contract-chart svg{display:block;width:100%;height:auto;background:transparent;border-radius:6px;touch-action:pan-y}
.contract-chart [hidden]{display:none}
.contract-chart svg:focus-visible{outline:2px solid #60a5fa}
.contract-chart-legend{display:flex;gap:8px;font-size:11px;margin-bottom:8px}
.contract-chart-legend button{background:transparent;border:1px solid #64748b;color:#94a3b8;padding:4px 10px;border-radius:6px}
.contract-chart-legend button[aria-pressed="true"]{background:#1b2735;border-color:var(--blue);color:var(--blue)}
.purchase-input-row{flex-basis:100%;display:flex;align-items:center;gap:8px;flex-wrap:nowrap;overflow-x:auto}
.purchase-input-row>*{flex-shrink:0}
.monitor-info .close-input-row,.monitor-info .stop-loss-row{flex-wrap:wrap;overflow:visible;gap:8px 12px;min-width:0;width:100%;box-sizing:border-box}
#closeSettingsPanel .contract-field{flex-wrap:nowrap;max-width:100%}
.monitor-info #closeSettingsPanel #closeTimeInForceInput{width:168px}
.monitor-info .stop-loss-row input[type=number]{width:44px;height:30px;box-sizing:border-box;padding:4px}
.monitor-info .stop-loss-row #stopLossTimeInForceInput{width:168px}
.monitor-info .purchase-trend-row #timeInForceInput{width:168px}
.monitor-info .purchase-trend-row #minLiveStrengthInput{width:126px}
.monitor-info .purchase-trend-row #contractTrendChangesInput{width:42px;height:30px;box-sizing:border-box}
.monitor-info .fallback-inputs input[type=checkbox]{width:auto}
.purchase-minute-group{display:flex;align-items:center;gap:5px;white-space:nowrap;font-size:11px}
.monitor-info .purchase-input-row input{width:36px;padding:6px 4px;appearance:textfield;-moz-appearance:textfield}
.purchase-input-row input::-webkit-inner-spin-button,.purchase-input-row input::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.monitor-info .purchase-input-row #initialBalanceInput{width:46px}
.order-conditions>summary{cursor:pointer;color:var(--muted);font-size:11px}
.order-conditions[open]>summary{margin-bottom:8px}
.purchase-conditions label{display:inline-flex;align-items:center;gap:4px;margin:5px}
.contract-settings .purchase-conditions input[type=checkbox]{width:auto}
.purchase-conditions select{max-width:100%;padding:5px}
.purchase-conditions{min-width:0;overflow-x:auto}
.purchase-filter-sides{display:flex;gap:18px;align-items:flex-end;margin-top:5px;width:max-content;min-width:100%}
.purchase-filter-sides>*{flex-shrink:0}
.purchase-agreement-side>label{display:block;margin:0 0 6px;font-size:12px}
.purchase-agreement-side select{width:auto;field-sizing:fixed;font-size:13px;box-sizing:border-box}
.purchase-model-side{font-size:12px}
.purchase-model-options{display:flex;align-items:center;gap:8px;white-space:nowrap;margin-top:6px}
.purchase-model-options label{margin:0;gap:4px}
.round-setup-row{display:flex;flex-direction:column;gap:8px}
.round-setup-main{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.round-setup-row #roundSetup{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:0!important}
.round-setup-row #roundSetup>span{white-space:nowrap}
.round-setup-row .round-setup-sources{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0}
.round-setup-row .round-setup-sources>summary{cursor:pointer;color:var(--muted);font-size:11px;margin:0!important}
.round-setup-row #quotes{display:flex;align-items:center;flex-wrap:wrap}
.monitor-info .round-setup-row #roundSetup{margin-top:0!important}
.monitor-info #roundSetup,.monitor-info #moves{margin-top:4px!important}
.monitor-info summary{cursor:pointer;color:var(--muted);font-size:11px}
.monitor-info details[open]>summary{margin-bottom:5px}
.monitor-info .secondary-details{margin-top:6px}
.monitor-info #robinhoodStatus{font-size:11px;overflow-wrap:anywhere}
.fallback-models{display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:12px}
#fallbackRules{flex-basis:100%;width:100%;min-width:0}
.primary-rule,.fallback-rule{border:1px solid var(--border,#394150);border-radius:8px;padding:12px;margin:12px 0;width:100%;min-width:0;box-sizing:border-box}
.primary-rule .purchase-input-row,#fallbackRules .purchase-input-row{flex-wrap:wrap;overflow:visible;min-width:0;gap:8px}
#fallbackRules .contract-field{max-width:100%;gap:5px}
#fallbackRules input[type=checkbox]{width:auto}
#fallbackRules select{width:168px;max-width:100%;min-width:0;height:30px;box-sizing:border-box;padding:4px 6px;font-size:12px;border-radius:8px}
#fallbackRules .fallback-models{min-width:0;max-width:100%}
.primary-rule{flex-basis:100%}
.primary-rule .purchase-filter-sides{width:100%;min-width:0;flex-wrap:wrap;gap:12px 18px}
.primary-rule .purchase-filter-sides>*{max-width:100%;min-width:0}
.primary-rule .purchase-model-options{flex-wrap:wrap}
.fallback-actions{display:flex;justify-content:flex-end;gap:4px;margin-top:10px}
.fallback-rule{position:relative;display:block}
.fallback-details>summary{padding-right:124px;min-height:30px;box-sizing:border-box}
.fallback-details{flex:1;min-width:0}
.fallback-details[open]{flex-basis:100%}
.monitor-info .fallback-details>summary{display:flex;align-items:center;gap:6px;color:var(--text);font-size:11px;cursor:pointer;list-style:none}
.fallback-details>summary::-webkit-details-marker{display:none}
.fallback-details>summary::before{content:'▸';flex-shrink:0;color:var(--muted)}
.fallback-details[open]>summary::before{content:'▾'}
.fallback-content{margin-top:8px}
.fallback-rule>.fallback-actions{position:absolute;top:12px;right:12px;margin:0}
.fallback-rule:has(.fallback-details:not([open])) [aria-label="Add fallback"]{display:none}



.monitor-info .fallback-details>summary{font-size:12px;line-height:1.6;gap:8px}
.fallback-header-text{white-space:normal}
.fallback-header-text .fallback-number{color:#a5b4fc;font-weight:600;margin-right:6px}
.fallback-header-text [data-summary]{display:inline-block;margin:0 3px;font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
.fallback-header-text [data-summary="minute_from"]{color:#67e8f9}
.fallback-header-text [data-summary="minute_to"]{color:#fcd34d}
.fallback-header-text [data-summary="contracts"]{color:#86efac}
.trade-header-part{display:inline-block;white-space:nowrap}
.fallback-header-text [data-summary="close"]{color:#86efac}
.fallback-header-text [data-summary="stop"]{color:#fcd34d}
.header-price-range{display:inline-block;white-space:nowrap;margin-left:6px}
.fallback-header-text [data-summary="min_price_cents"],.fallback-header-text [data-summary="max_price_cents"]{color:#f9a8d4}

/* One spacing scale for all trade condition groups. */
#orderSettingsPanel .contract-settings{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;margin-top:12px}
#orderSettingsPanel .primary-rule,#orderSettingsPanel .fallback-rule{margin:0;padding:12px}
#orderSettingsPanel .primary-rule,#orderSettingsPanel .fallback-content{display:grid;grid-template-columns:minmax(0,1fr);gap:12px}
#orderSettingsPanel #fallbackRules{display:grid;gap:12px}
#orderSettingsPanel #fallbackRules:empty{display:none}
#orderSettingsPanel .purchase-input-row{width:100%;min-width:0;box-sizing:border-box;flex-wrap:wrap;overflow:visible;gap:8px 12px}
#orderSettingsPanel{--condition-indent:19px}
#orderSettingsPanel .force-buy-inputs,#orderSettingsPanel .stop-loss-row,#orderSettingsPanel .fallback-exit-row{padding-left:var(--condition-indent)}
#orderSettingsPanel .force-buy-inputs>.contract-field:first-child,#orderSettingsPanel .stop-loss-row>.contract-field:first-child,#orderSettingsPanel .fallback-exit-row>.contract-field:first-child{margin-left:calc(-1 * var(--condition-indent))}
#orderSettingsPanel .choppiness-inputs{padding-left:var(--condition-indent)}
#orderSettingsPanel .choppiness-inputs>.contract-field:first-child{margin-left:calc(-1 * var(--condition-indent))}
#orderSettingsPanel .choppiness-round-controls>.contract-field:first-child{margin-left:calc(-1 * var(--condition-indent))}
#orderSettingsPanel .contract-field,#orderSettingsPanel .purchase-minute-group{gap:6px;max-width:100%}
#orderSettingsPanel .purchase-filter-sides{margin:0;gap:12px 16px;align-items:center}
#orderSettingsPanel .purchase-agreement-side{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
#orderSettingsPanel .purchase-agreement-side>label{display:inline;margin:0;white-space:nowrap}
#orderSettingsPanel .purchase-model-options{margin-top:0;gap:8px 12px;min-height:30px;font-size:12px;line-height:1.35}
#orderSettingsPanel .purchase-model-options label{display:inline-flex;align-items:center;margin:0;gap:6px;line-height:1.35}
#orderSettingsPanel .fallback-models{flex-basis:auto;gap:8px 12px;font-size:11px}
#orderSettingsPanel .purchase-input-row input[type=number]{width:40px;height:30px;box-sizing:border-box;padding:4px}
#orderSettingsPanel input[type=checkbox]{width:13px;height:13px;margin:0;flex-shrink:0;vertical-align:middle}
#orderSettingsPanel select{height:30px;box-sizing:border-box;padding:4px 6px;font-size:12px;border-radius:8px}
#orderSettingsPanel select:is([id$="TimeInForceInput"],#timeInForceInput,[data-key="time_in_force"]){width:190px;min-width:0;max-width:100%;height:30px;box-sizing:border-box;padding:4px 6px;font-size:12px;border-radius:8px}
#orderSettingsPanel .fallback-details[open]>summary{margin-bottom:0}
#orderSettingsPanel .fallback-content{margin-top:12px}
#primaryExitRules{display:grid;gap:12px;min-width:0}
#orderSettingsPanel .primary-details>summary{padding-right:0}
#orderSettingsPanel .order-save-row{margin:0}
#orderSettingsPanel #orderSettingsStatus:empty{display:none}

.order-status-actions{display:flex;align-items:center;gap:8px}
#orderLogsDialog{position:fixed;inset:0 0 0 auto;margin:0;height:100dvh;max-height:100dvh;width:min(480px,100vw);max-width:100vw;box-sizing:border-box;padding:20px;border:0;border-left:1px solid var(--line);background:var(--card,#161e28);color:var(--text);overflow:hidden}
#orderLogsDialog[open]{display:flex;flex-direction:column;gap:12px}
#orderLogsDialog::backdrop{background:rgba(0,0,0,.45)}
#orderLogsDialog .order-status-log{flex:1;min-height:0}
.order-status-log{overflow-y:auto;scrollbar-gutter:stable;overscroll-behavior:contain;margin-top:8px;padding-right:6px}
.order-status-log .order-message{margin-top:0;margin-bottom:8px;font-size:12px;overflow-wrap:anywhere}
.order-status-time{display:block;color:var(--muted);font-size:10px;margin-bottom:3px}
.order-message{white-space:pre-line;margin-top:8px;padding:10px 12px;border-left:3px solid var(--blue);border-radius:6px;background:#669cff0d;color:var(--text);line-height:1.5}
.order-message[data-kind="waiting"]{border-color:var(--yellow);background:#f5c4510d}
.order-message[data-kind="skipped"]{border-color:var(--muted);background:#8ea0b50d}
.order-message[data-kind="success"]{border-color:var(--green);background:#30d27c0d}
.order-message[data-kind="error"]{border-color:var(--red);background:#ff5c6c0d}
.monitor-info #btDetails{font-size:11px}
.backtest-card{overflow-x:auto}
.backtest-row{display:flex;align-items:center;gap:14px;white-space:nowrap;min-width:max-content}
.backtest-row+.backtest-row{border-top:1px solid var(--line);margin-top:8px;padding-top:8px}
.backtest-row>*{flex-shrink:0}
.monitor-info .backtest-row #moves{margin-top:0!important}
.monitor-info .direction .muted{font-size:11px;margin-top:4px!important}
.monitor-info .direction .spread{margin-top:4px!important;font-size:11px}

.header{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:center}
.header-text-row{display:flex;flex-direction:column;align-items:flex-start;justify-content:center;gap:4px;overflow:visible;white-space:normal}
.header-text-row>.subtitle{text-align:left}
.header-round{justify-content:flex-end;min-width:0}
.round-countdown{justify-self:center;color:var(--muted);font-size:32px;font-weight:900;line-height:1;letter-spacing:.03em;font-variant-numeric:tabular-nums;white-space:nowrap}
@media(max-width:850px){
  .header{grid-template-columns:1fr;gap:8px;align-items:center}
  .header-text-row{align-items:center;text-align:center}
  .header-text-row>.subtitle{text-align:center}
  .header-round{justify-content:center;justify-self:center}
  .round-countdown{font-size:28px}
}

</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="header-text-row">
      <div class="title">BTC TargetPulse</div>
      <div class="subtitle">Robinhood · <span id="assetTitle">BTC</span>/USD · 15m · Live</div>
    </div>
    <div id="headerRoundTimer" class="round-countdown" role="timer" aria-live="off" aria-label="Time remaining in current round">--:--</div>
    <div class="row header-round">
      <select id="assetSelect" onchange="setAsset(this.value)" hidden>
        <option value="BTC">BTC</option><option value="ETH">ETH</option><option value="SOL">SOL</option>
        <option value="XRP">XRP</option><option value="DOGE">DOGE</option>
        <option value="BNB">BNB</option><option value="HYPE">HYPE</option>
      </select>
      <div class="badge" id="roundBadge">Loading…</div>
    </div>
  </div>

  <div class="monitor-layout">
  <div class="grid monitor-info">
    <div class="price-target-row span12">
    <div class="card" id="latestPriceCard">
      <div class="label" id="priceSource">Latest price</div>
      <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"><div class="big" id="livePrice">—</div><span id="livePriceFreshness" class="muted" style="font-size:11px"></span></div>
      <div id="liveDelta" class="muted"></div>
    </div>

    <div class="card" id="targetCard">
      <div class="label">Robinhood target</div>
      <div class="big cyan" id="targetDisplay">Not entered</div>
      <div id="timer" class="muted" hidden>—</div>
    </div>
    </div>

    <div class="card span12" id="accuracyCard">
      <div class="accuracy-comparison" title="Accuracy of filtered history, excluding current and future rounds">
      <div><div class="label">Tracked accuracy · purchases</div>
      <div class="big" id="trackedAccuracy">—</div>
      <div class="muted"><span id="trackedDetail">No settled filled purchases</span> · Total P/L: <strong id="trackedTotalPnl">—</strong></div></div>
      <div><div class="label">Tracked accuracy · final prediction</div>
      <div class="big" id="finalAccuracy">—</div>
      <div id="finalAccuracyDetail" class="muted">No settled predictions</div></div>
      </div>
    </div>

    <section class="card span12" id="underlyingChartSection" aria-labelledby="underlyingChartHeading">
      <div class="spread" id="underlyingChartHeading"><button class="section-toggle" id="toggleUnderlyingBtn" type="button" aria-label="Hide price chart" aria-controls="underlyingPriceChart" aria-expanded="true" onclick="toggleDashboardSection('underlying')"><span class="section-arrow" aria-hidden="true">▾</span><span class="label" id="underlyingChartTitle">Bitcoin price during this round</span></button><button type="button" id="forceBuyBtn" class="secondary" onclick="requestForceBuy()" disabled>Force Buy</button></div>
      <div class="purchase-input-row" style="margin-top:2px;margin-bottom:2px">
        <label class="contract-field" style="color:var(--blue)"><input id="showBitcoinLine" type="checkbox" checked style="width:auto" onchange="setMarketLineVisibility()">Bitcoin</label>
        <label class="contract-field" style="color:var(--green)"><input id="showContractLine" type="checkbox" checked style="width:auto" onchange="setMarketLineVisibility()">Contract Yes</label>
        <span class="muted" style="color:var(--yellow)">— Target</span>
      </div>
      <div id="mergedQuotes">
      <div id="contractPrices" class="muted"><span><strong aria-label="Yes contract">↑&nbsp;</strong><span id="contractYesPrice">—</span></span><span><strong aria-label="No contract">↓&nbsp;</strong><span id="contractNoPrice">—</span></span><span id="contractPriceTime"></span></div>
      </div>
      <div id="bitcoinVolatility" class="muted" style="margin:6px 0" aria-live="polite">Range: waiting for samples…</div>
      <div id="underlyingPriceChart"></div>
    </section>


    <section class="card span12" id="orderStatusSection" aria-labelledby="orderStatusHeading">
      <div class="spread"><div class="label" id="orderStatusHeading">Order status</div><div class="order-status-actions"><button type="button" class="secondary" id="confirmClosedStatus" hidden>Confirm closed</button><span class="badge" id="orderStatusKind">Info</span><button type="button" class="secondary" aria-haspopup="dialog" aria-controls="orderLogsDialog" onclick="openOrderLogs()">More</button></div></div>
      <div id="robinhoodStatus" class="order-message" data-kind="info" role="status" aria-live="polite"></div>
    </section>

    <section class="card span12" id="balanceSection" aria-labelledby="balanceHeading">
      <div class="spread" id="balanceHeading"><button class="section-toggle" id="toggleBalanceBtn" type="button" aria-label="Show balance" aria-controls="balanceContent" aria-expanded="false" onclick="toggleDashboardSection('balance')"><span class="section-arrow" aria-hidden="true">▸</span><span class="label">Balance</span></button><div class="balance-compact" id="balanceCompact"><b class="cyan" id="balanceCompactValue">—</b><span>P/L <b id="balanceCompactPnl">—</b></span></div></div>
      <div id="balanceContent" hidden>
      <div class="balance-summary">
        <label class="contract-field" for="initialBalanceInput">Start balance $ <input id="initialBalanceInput" type="number" min="0" step="0.01" oninput="balanceDirty=true"></label>
        <button class="secondary" id="saveBalanceBtn" type="button" onclick="saveBalance()">Save balance</button>
        <span class="current-balance-highlight">Current balance <b id="currentBalance">—</b></span>
        <span class="balance-pnl">P/L <b id="netPnl">—</b></span>
      </div>
      <div class="muted" id="balanceDetail"></div>
      <div class="muted" id="balanceStatus" role="status"></div>
      </div>
    </section>



    <!-- Temporarily hidden; remove hidden to restore manual target override. -->
    <div class="card span12" id="manualTargetOverride" hidden>
      <div class="spread">
        <div>
          <div class="label">Manual target override</div>
          <div class="medium" id="roundText" hidden>—</div>
        </div>
        <div class="target-form">
          <input id="targetInput" type="number" step="0.01" placeholder="e.g. 84760.39">
          <button id="targetBtn" onclick="setTarget()">Freeze Prediction</button>
        </div>
      </div>
      <div id="analysisStatus" class="status" style="margin-top:12px">Loading historical data…</div>
    </div>

    <div class="card span7">
      <div class="label">Frozen decision</div>
      <div id="decisionBox" class="direction">
        <div class="decision muted">WAITING FOR TARGET</div>

      </div>
    </div>

    <div class="card span5">
      <div class="label">Live minute recommendation</div>
      <div id="liveRecommendation" class="direction">
        <div class="decision muted">WAITING</div>

      </div>
    </div>


    <div class="card span8">
      <div class="spread">
        <button class="section-toggle" id="toggleModelsBtn" type="button" aria-label="Hide models" aria-controls="modelsContent" aria-expanded="true" onclick="toggleDashboardSection('models')"><span class="section-arrow" aria-hidden="true">▾</span><span class="label">Models</span></button>
      </div>
      <div id="modelsContent">
        <div class="muted" style="font-size:12px">Probability · adaptive weight · BT accuracy</div>
      <table class="models">
        <thead><tr><th>Model</th><th>Above</th><th>Weight</th><th>BT Acc</th></tr></thead>
        <tbody id="modelsBody"><tr><td colspan="4" class="muted">Waiting for prediction…</td></tr></tbody>
      </table>
      </div>
    </div>

    <div class="card span12">
      <details class="order-conditions" id="orderSettingsPanel"><summary>Trade Conditions</summary>
      <div class="purchase-input-row"><label class="contract-field"><input id="requireFinalLiveInput" type="checkbox" onchange="orderSettingsDirty=true"> Require Final + fresh Live agreement</label><label class="contract-field">Models agreeing ≥ <select id="minModelAgreementInput" onchange="orderSettingsDirty=true"><option value="0">Off</option><option value="1">1 of 4</option><option value="2">2 of 4</option><option value="3">3 of 4</option><option value="4">4 of 4</option></select></label><span class="muted">Applies to Primary, all fallbacks and Force Buy.</span></div>
      <label><input id="testModeInput" type="checkbox" onchange="orderSettingsDirty=true"> Test mode — virtual orders only (estimated fills and fees)</label>
      <div class="muted">When the accuracy pause is enabled and accuracy is below its limit, buys automatically become virtual even with Test mode off. Real buys resume when accuracy recovers.</div>
      <div class="contract-settings">
      <div class="primary-rule">
      <details class="fallback-details primary-details">
      <summary><input id="primaryEnabledInput" type="checkbox" checked aria-label="Primary" onclick="event.stopPropagation()" onchange="orderSettingsDirty=true"><span class="fallback-header-text"></span></summary>
      <div class="fallback-content primary-content">
      <div class="purchase-input-row primary-entry-inputs">
      <label class="contract-field" for="purchaseMinuteFromInput">Min From <input id="purchaseMinuteFromInput" type="number" min="0" max="15" step="0.1" oninput="orderSettingsDirty=true;updatePrimarySummary()"></label>
      <label class="contract-field" for="purchaseMinuteToInput">To <input id="purchaseMinuteToInput" type="number" min="0" max="15" step="0.1" oninput="orderSettingsDirty=true;updatePrimarySummary()"></label>
      <label class="contract-field" for="minPriceInput">Price &gt; ¢ <input id="minPriceInput" type="number" min="0" max="99.99" step="0.01" oninput="orderSettingsDirty=true;updatePrimarySummary()"></label>
      <label class="contract-field" for="maxPriceInput">&lt; ¢ <input id="maxPriceInput" type="number" min="0.01" max="100" step="0.01" oninput="orderSettingsDirty=true;updatePrimarySummary()"></label>
      <label class="contract-field" for="contractsInput">CTs <input id="contractsInput" type="number" min="1" step="1" oninput="orderSettingsDirty=true;updatePrimarySummary()"></label>
      <label class="contract-field" for="contractTrendChangesInput" title="Number of distinct price changes used to calculate the overall upward trend; N changes require N+1 quotes">Changes <input id="contractTrendChangesInput" type="number" min="1" step="1" value="10" oninput="orderSettingsDirty=true"></label>
      <div class="purchase-agreement-side">
        <label for="agreementBasisInput">Agree with</label>
        <select id="agreementBasisInput" onchange="orderSettingsDirty=true"><option value="final">Final Prediction</option><option value="live">Current Prediction</option><option value="either">Final or Current Prediction</option><option value="both">Final and Current Prediction</option></select>
        </div>
      <label class="contract-field" for="minLiveStrengthInput">Strength ≥ <select id="minLiveStrengthInput" onchange="orderSettingsDirty=true"><option>EXTREMELY WEAK</option><option>VERY WEAK</option><option>WEAK</option><option selected>MODERATE</option><option>STRONG</option></select></label>
      <div class="purchase-model-options">
        <span>With Models :</span>
        <label><input type="checkbox" name="requiredModel" value="Pattern" checked onchange="orderSettingsDirty=true"> Pattern</label>
        <label><input type="checkbox" name="requiredModel" value="Recent" checked onchange="orderSettingsDirty=true"> Recent</label>
        <label><input type="checkbox" name="requiredModel" value="Bootstrap" checked onchange="orderSettingsDirty=true"> Bootstrap</label>
        <label><input type="checkbox" name="requiredModel" value="Trend" checked onchange="orderSettingsDirty=true"> Trend</label>
        </div>
      </div>
      <div id="primaryExitRules"></div>
      </div>
      </details>
      </div>
      <div id="fallbackRules"></div>
      <div class="purchase-input-row purchase-trend-row">
      <label class="contract-field">Buy Time in force <select id="timeInForceInput" onchange="orderSettingsDirty=true"><option value="ioc">Immediate or cancel (IOC)</option><option value="good_till_date">Good till date</option></select></label>
      </div>
      <div class="purchase-input-row" title="Applies to Primary and Fallback, including virtual buys. Force Buy bypasses this condition. BTC must stay strictly on the purchased side of the target. Touches, crossings and quote gaps over 10 seconds reset the timer.">
        <label class="contract-field"><input id="entryStabilityEnabled" type="checkbox" style="width:auto" onchange="orderSettingsDirty=true">Require BTC on buy side of target for</label>
        <label class="contract-field"><input id="entryStabilitySeconds" aria-label="Minimum BTC stability seconds" type="number" min="0.1" max="900" step="any" value="60" oninput="orderSettingsDirty=true">seconds before Primary / Fallback buys</label><span id="entryStabilityStatus" class="muted"></span>
      </div>
      <div class="purchase-input-row"><label class="contract-field"><input id="accuracyBuyGuardEnabled" type="checkbox" style="width:auto" onchange="orderSettingsDirty=true">Pause buys if Final prediction accuracy &lt;</label><label class="contract-field"><input id="accuracyBuyGuardMinInput" type="number" min="0" max="100" step="0.1" value="60" oninput="orderSettingsDirty=true">%</label><label class="contract-field">over last <input id="accuracyBuyGuardHoursInput" type="number" min="0.01" max="8760" step="any" value="24" oninput="orderSettingsDirty=true">hours</label><span id="accuracyBuyGuardStatus" class="muted"></span></div>
      <div class="purchase-input-row choppiness-inputs" title="Score = number of midpoint crossings in 15-second samples: Yes crosses 50 cents; Bitcoin crosses this round target. First observed side and exact touches do not count. Gaps are ignored; at least four sampled prices are needed. Uses exactly the last N completed rounds, including unbought rounds. Current score is provisional. The average filter pauses buys if required round scores are missing. Current/following-round filters act on observed crossings. Range is shown separately and does not gate buying.">
        <label class="contract-field"><input id="choppinessBuyGuardEnabled" type="checkbox" style="width:auto" onchange="orderSettingsDirty=true">Pause buys if average midpoint crossings &gt;</label>
        <label class="contract-field"><input id="choppinessBuyGuardMaxInput" aria-label="Maximum average midpoint crossings" type="number" min="0" step="any" value="3" oninput="orderSettingsDirty=true"></label>
        <label class="contract-field">over last <input id="choppinessBuyGuardRoundsInput" aria-label="Completed rounds for midpoint crossings" type="number" min="1" max="96" step="1" value="3" oninput="orderSettingsDirty=true">completed rounds</label>
        <label class="contract-field">Source <select id="choppinessBuyGuardSourceInput" onchange="orderSettingsDirty=true"><option value="bitcoin">Bitcoin (BTC)</option><option value="contract_yes">Contract Yes</option></select></label>
        <div class="purchase-input-row choppiness-round-controls" title="Pause new buys for the rest of the current round once this observed crossing count is reached. Uses the selected source, including Force Buy.">
          <label class="contract-field"><input id="choppinessCurrentEnabled" type="checkbox" onchange="orderSettingsDirty=true">Pause current round at ≥</label>
          <label class="contract-field"><input id="choppinessCurrentLimitInput" aria-label="Current-round midpoint crossing limit" type="number" min="1" max="59" step="1" value="7" oninput="orderSettingsDirty=true">crossings</label>
        </div>
        <div class="purchase-input-row choppiness-round-controls" title="If the immediately previous round reached this limit, block new buys for this whole round. Resets on the next round; repeats only if another round also reaches the limit. Existing exit conditions continue working.">
          <label class="contract-field"><input id="choppinessBlockNextInput" type="checkbox" onchange="orderSettingsDirty=true">Block following round after this limit is reached</label>
        </div>
        <span id="choppinessBuyGuardStatus" class="muted" style="flex-basis:100%;white-space:normal;flex-shrink:1"></span>
      </div>
      <div class="purchase-input-row"><label class="contract-field"><input id="allowMultiBuyInput" type="checkbox" style="width:auto" onchange="orderSettingsDirty=true">Allow multiple buys in the same round after full close</label></div>
      <div class="purchase-input-row fallback-inputs force-buy-inputs">
        <label class="contract-field"><input id="forceBuyEnabled" type="checkbox" onchange="orderSettingsDirty=true">Force Buy</label>
        <label class="contract-field">Min From<input id="forceBuyMinuteFromInput" type="number" min="0" max="15" step=".01" value="0" oninput="orderSettingsDirty=true"></label>
        <label class="contract-field">To<input id="forceBuyMinuteToInput" type="number" min="0" max="15" step=".01" value="15" oninput="orderSettingsDirty=true"></label>
        <label class="contract-field">BRTI Δ &gt; $<input id="forceBuyBrtiDeltaInput" type="number" placeholder="Off" title="Blank disables the directional BRTI gap condition" step=".01" value="" oninput="orderSettingsDirty=true"></label>
        <label class="contract-field">Price &gt; ¢<input id="forceBuyMinPriceInput" type="number" min="0" max="100" step=".01" value="0" oninput="orderSettingsDirty=true"></label>
        <label class="contract-field">&lt; ¢<input id="forceBuyMaxPriceInput" type="number" min="0" max="100" step=".01" value="100" oninput="orderSettingsDirty=true"></label>
        <label class="contract-field">CTs<input id="forceBuyContractsInput" type="number" min="1" step="1" value="1" oninput="orderSettingsDirty=true"></label>
        <label class="contract-field">Time in force <select id="forceBuyTimeInForceInput" onchange="orderSettingsDirty=true"><option value="ioc">Immediate or cancel (IOC)</option><option value="good_till_date">Good till date</option></select></label>
      </div>
        <div class="purchase-input-row stop-loss-row">
          <label class="contract-field"><input id="forceCloseEnabled" type="checkbox" style="width:auto" onchange="orderSettingsDirty=true">Force Close</label>
          <label class="contract-field">Price &gt; (¢) <input id="forceCloseMinPriceInput" type="number" min="0" max="99.99" step=".01" value="0" oninput="orderSettingsDirty=true"></label>
          <label class="contract-field" title="Directional BRTI gap for the held side; blank means Off">BRTI Δ &lt; $ <input id="forceCloseBrtiDeltaInput" type="number" step=".01" placeholder="Off" oninput="orderSettingsDirty=true"></label>
          <label class="contract-field">Min From <input id="forceCloseMinuteFromInput" type="number" min="0" max="15" step=".1" value="0" oninput="orderSettingsDirty=true"></label>
          <label class="contract-field">To <input id="forceCloseMinuteToInput" type="number" min="0" max="15" step=".1" value="15" oninput="orderSettingsDirty=true"></label>
          <label class="contract-field">Time in force <select id="forceCloseTimeInForceInput" onchange="orderSettingsDirty=true"><option value="ioc">Immediate or cancel (IOC)</option><option value="good_till_date">Good till date</option></select></label>
        </div>
        <div class="order-save-row"><button id="saveOrderSettingsBtn" onclick="saveOrderSettings()">Save</button></div><span id="orderSettingsStatus" class="muted" role="status"></span>
      </div>
      </details>
    </div>
    <div class="card span4 backtest-card">
      <div class="backtest-row">
        <div class="label">Walk-forward backtest</div>
        <div class="big" id="btAccuracy">—</div>
        <div id="btDetails" class="muted">—</div>
      </div>
      <div class="backtest-row">
        <div class="label">Similar 15m moves</div>
        <div id="moves" class="muted">—</div>
      </div>
    </div>

    <div class="card span12">
      <details><summary>Data status and reload</summary>
      <div class="spread">
        <div id="historyStatus" class="muted">History: loading…</div>
        <button class="secondary" onclick="reloadHistory()">Reload History</button>
      </div>
      <div class="warning" id="warningText" style="margin-top:10px"></div>
      </details>
    </div>
  </div>
    <div class="card monitor-history">
      <div class="history-header-row">
      <div class="label">Round history · all models</div>
      <div id="roundHistoryCount" class="muted"></div>
      </div>
      <div class="history-filter-toolbar">
      <label class="muted history-purchase-filter">Purchases
        <select id="roundPurchaseFilter" onchange="roundHistoryPage=1;loadRoundHistory()" style="padding:6px 8px;font-size:12px">
          <option value="all">All rounds</option><option value="only">Purchased rounds only</option><option value="hide">Hide purchased rounds</option>
        </select>
      </label>

        <label class="contract-field">From <input id="roundDateFrom" type="date" style="width:145px;padding:6px;font-size:12px" onchange="roundHistoryPage=1;loadRoundHistory()"></label>
        <label class="contract-field">To <input id="roundDateTo" type="date" style="width:145px;padding:6px;font-size:12px" onchange="roundHistoryPage=1;loadRoundHistory()"></label>
        <button class="secondary" onclick="document.getElementById('roundDateFrom').value='';document.getElementById('roundDateTo').value='';roundHistoryPage=1;loadRoundHistory()">Clear dates</button>
      </div>
      <div class="round-log-scroll"><table class="round-log">
        <thead><tr><th>Round / status</th><th>Target price</th><th>Final prediction<select class="history-column-filter" data-column="final" aria-label="Filter Final prediction" onchange="roundHistoryPage=1;loadRoundHistory()"><option value="all">All</option><option value="up">↑</option><option value="down">↓</option><option value="hit">✓</option><option value="miss">✕</option></select></th><th>Pattern<select class="history-column-filter" data-column="pattern" aria-label="Filter Pattern" onchange="roundHistoryPage=1;loadRoundHistory()"><option value="all">All</option><option value="up">↑</option><option value="down">↓</option><option value="hit">✓</option><option value="miss">✕</option></select></th><th>Recent<select class="history-column-filter" data-column="recent" aria-label="Filter Recent" onchange="roundHistoryPage=1;loadRoundHistory()"><option value="all">All</option><option value="up">↑</option><option value="down">↓</option><option value="hit">✓</option><option value="miss">✕</option></select></th><th>Bootstrap<select class="history-column-filter" data-column="bootstrap" aria-label="Filter Bootstrap" onchange="roundHistoryPage=1;loadRoundHistory()"><option value="all">All</option><option value="up">↑</option><option value="down">↓</option><option value="hit">✓</option><option value="miss">✕</option></select></th><th>Trend<select class="history-column-filter" data-column="trend" aria-label="Filter Trend" onchange="roundHistoryPage=1;loadRoundHistory()"><option value="all">All</option><option value="up">↑</option><option value="down">↓</option><option value="hit">✓</option><option value="miss">✕</option></select></th><th>Live at purchase<select class="history-column-filter" data-column="live" aria-label="Filter Live at purchase" onchange="roundHistoryPage=1;loadRoundHistory()"><option value="all">All</option><option value="up">↑</option><option value="down">↓</option><option value="hit">✓</option><option value="miss">✕</option></select></th><th>Settlement<select class="history-column-filter" data-column="actual" aria-label="Filter Settlement" onchange="roundHistoryPage=1;loadRoundHistory()"><option value="all">All</option><option value="up">↑</option><option value="down">↓</option></select></th><th>Purchase</th></tr></thead>
        <tbody id="roundHistoryBody"><tr><td colspan="10">Loading rounds…</td></tr></tbody>
      </table></div>
      <div class="spread" style="margin-top:12px"><button class="secondary" id="roundHistoryPrev" onclick="changeRoundHistoryPage(-1)">Newer</button><span id="roundHistoryPage" class="muted"></span><button class="secondary" id="roundHistoryNext" onclick="changeRoundHistoryPage(1)">Older</button></div>
    </div>
  </div>
</div>

<dialog id="orderLogsDialog" aria-labelledby="orderLogsHeading">
  <div class="spread"><strong id="orderLogsHeading">All order status logs</strong><button type="button" class="secondary" aria-label="Close order status logs" onclick="document.getElementById('orderLogsDialog').close()">×</button></div>
  <div class="spread"><button type="button" class="secondary" onclick="loadOrderLogs()">Latest</button><span id="orderLogsPageStatus" class="muted" role="status"></span><button type="button" id="olderOrderLogsBtn" class="secondary" onclick="loadOlderOrderLogs()">Older logs</button></div>
  <div id="orderStatusLogs" class="order-status-log" role="log" aria-label="Order status history" aria-live="polite" aria-relevant="additions" tabindex="0"></div>
</dialog>
<dialog id="roundChartDialog" aria-labelledby="roundChartDialogTitle">
  <div class="spread"><strong id="roundChartDialogTitle">Round chart</strong><button type="button" class="secondary" onclick="document.getElementById('roundChartDialog').close()" aria-label="Close round chart">×</button></div>
  <div id="roundChartDialogContent"></div>
</dialog>
<script>
let lastRoundStart = null;
const dashboardSections={underlying:{content:'underlyingPriceChart',button:'toggleUnderlyingBtn',label:'price chart'},balance:{content:'balanceContent',button:'toggleBalanceBtn',label:'balance',defaultHidden:true},models:{content:'modelsContent',button:'toggleModelsBtn',label:'models'}};
function setDashboardSectionHidden(name,hidden){
  const section=dashboardSections[name];
  document.getElementById(section.content).hidden=hidden;
  if(name==='balance')document.getElementById('balanceCompact').hidden=!hidden;
  const button=document.getElementById(section.button);
  button.querySelector(".section-arrow").textContent=hidden?"▸":"▾";
  button.setAttribute("aria-label",`${hidden?'Show':'Hide'} ${section.label}`);
  button.setAttribute('aria-expanded',String(!hidden));
}
function toggleDashboardSection(name){
  const hidden=!document.getElementById(dashboardSections[name].content).hidden;
  setDashboardSectionHidden(name,hidden);
  try{localStorage.setItem(`dashboard.${name}.hidden`,String(hidden));}catch{}
}
for(const name of Object.keys(dashboardSections)){
  try{const saved=localStorage.getItem(`dashboard.${name}.hidden`);setDashboardSectionHidden(name,saved===null?!!dashboardSections[name].defaultHidden:saved==='true');}catch{setDashboardSectionHidden(name,!!dashboardSections[name].defaultHidden);}
}
const fmtMoney=v=>v==null?"—":"$"+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const clsSigned=v=>Number(v)>=0?"green":"red";
const fmtSigned=(v,d=2)=>(Number(v)>=0?"+":"")+Number(v).toFixed(d);

function secondsText(sec){
  sec=Math.max(0,Number(sec)||0);
  const m=Math.floor(sec/60),s=sec%60;
  return String(m).padStart(2,"0")+":"+String(s).padStart(2,"0");
}

let headerCountdownState=null;
function headerCountdownColor(seconds){
  // Elapsed round phases: 5m green, 4m blue, 3m white, 2m yellow, 1m red.
  if(seconds>600)return 'var(--green)';
  if(seconds>360)return 'var(--blue)';
  if(seconds>180)return 'var(--text)';
  if(seconds>60)return 'var(--yellow)';
  return 'var(--red)';
}
function tickHeaderCountdown(){
  const timer=document.getElementById('headerRoundTimer');
  if(!headerCountdownState){timer.textContent='--:--';timer.style.color='var(--muted)';return;}
  const elapsed=performance.now()-headerCountdownState.receivedAt;
  const seconds=Math.max(0,Math.floor((headerCountdownState.end-headerCountdownState.serverNow-elapsed)/1000));
  const text=secondsText(seconds);
  if(timer.textContent!==text)timer.textContent=text;
  timer.style.color=headerCountdownColor(seconds);
  timer.setAttribute('aria-label',`Time remaining in current round: ${text}`);
}
function syncHeaderCountdown(s){
  const end=Date.parse(s.round?.end),serverNow=Date.parse(s.server_time);
  headerCountdownState=Number.isFinite(end)&&Number.isFinite(serverNow)
    ?{end,serverNow,receivedAt:performance.now()}:null;
  tickHeaderCountdown();
}
setInterval(tickHeaderCountdown,1000);

let orderSettingsDirty=false, orderSettingsLoaded=false, balanceDirty=false;
let renderedOrderSettings=null;
const fallbackFields=[['minute_from','Min From'],['minute_to','To'],['min_distance_dollars','BRTI Δ > $'],['min_price_cents','Price > ¢'],['max_price_cents','< ¢'],['contracts','CTs']];
function readTradeExits(row){
  const exits={};
  for(const key of ['close_conditions','stop_loss']){
      exits[key]={};
      row.querySelectorAll(`[data-exit="${key}"]`).forEach(input=>{
        exits[key][input.dataset.key]=input.type==='checkbox'?input.checked:input.tagName==='SELECT'?input.value:input.value.trim()===''?null:Number(input.value);
      });
    }
  return exits;
}
function readFallbackRules(){
  return Array.from(document.querySelectorAll('#fallbackRules .fallback-rule'),row=>{
    const rule={enabled:row.querySelector('[data-field="enabled"]').checked};
    for(const [key] of fallbackFields){
      const value=row.querySelector(`[data-field="${key}"]`).value;
      rule[key]=key==='min_distance_dollars'&&value.trim()===''?null:Number(value);
    }
    rule.contract_trend_changes=Number(row.querySelector('[data-field="contract_trend_changes"]').value);
    rule.min_live_strength=row.querySelector('[data-field="min_live_strength"]').value;
    Object.assign(rule,readTradeExits(row));
    Object.defineProperty(rule,'_collapsed',{value:!row.querySelector('.fallback-details').open});
    rule.required_models=Array.from(row.querySelectorAll('[data-model]:checked'),input=>input.dataset.model);
    return rule;
  });
}
function renderFallbackExit(key,settings){
  const defaults=key==='close_conditions'?{enabled:false,profit_per_ct_cents:0,ct_price_above_cents:0,max_brti_delta_dollars:null,minute_from:5,minute_to:10,time_in_force:'ioc'}:{enabled:true,drop_cents:20,max_price_cents:100,max_brti_delta_dollars:null,minute_from:0,minute_to:15,time_in_force:'ioc'};
  const values=Object.fromEntries(Object.keys(defaults).map(key=>[key,settings?.[key]??defaults[key]]));
  const labels={profit_per_ct_cents:'Profit per CT > (¢)',ct_price_above_cents:'CT price > (¢)',min_price_cents:'Price > ¢',max_price_cents:'Price < ¢',drop_cents:'Drop from peak bid (¢)',max_brti_delta_dollars:'BRTI Δ < $',minute_from:'Min From',minute_to:'To'};
  return `<div class="purchase-input-row fallback-exit-row"><label class="contract-field"><input type="checkbox" data-exit="${key}" data-key="enabled" ${values.enabled?'checked':''} onchange="orderSettingsDirty=true;updateTradeSummary(this)">${key==='close_conditions'?'Close all':'Stop loss'}</label>${Object.entries(labels).filter(([field])=>field in values).map(([field,label])=>`<label class="contract-field" ${field==='profit_per_ct_cents'?'title="Estimated net profit per contract in cents after buy and sell fees"':''}>${label}<input type="number" step=".01" data-exit="${key}" data-key="${field}" value="${values[field]??''}" ${field==='max_brti_delta_dollars'?'placeholder="Off"':''} oninput="orderSettingsDirty=true;updateTradeSummary(this)"></label>`).join('')}<label class="contract-field">Time in force<select data-exit="${key}" data-key="time_in_force" onchange="orderSettingsDirty=true;updateTradeSummary(this)"><option value="ioc" ${values.time_in_force==='ioc'?'selected':''}>Immediate or cancel (IOC)</option><option value="good_till_date" ${values.time_in_force==='good_till_date'?'selected':''}>Good till date</option></select></label></div>`;
}
function renderTradeHeader(name,rule){
  const number=(key,value)=>`<span data-summary="${key}">${value==null||value===''?'—':Number(value)}</span>`;
  const close=rule.close_conditions||{},stop=rule.stop_loss||{};
  const exit=(label,key,enabled,arrow,value)=>`<span class="trade-header-part" title="${key==='close'?'Net profit per CT after fees':'Trailing drop from peak bid'}"><strong class="${enabled?'green':'red'}">${enabled?'✓':'✕'}</strong> ${label} <strong>${arrow}${number(key,value)}¢</strong></span>`;
  return `<span class="fallback-number">${name}:</span> <span class="trade-header-part">${number('contracts',rule.contracts)} CTs</span> - <span class="trade-header-part">Min ${number('minute_from',rule.minute_from)} To ${number('minute_to',rule.minute_to)}</span> - <span class="trade-header-part">Price &gt;${number('min_price_cents',rule.min_price_cents)}¢ &amp; &lt;${number('max_price_cents',rule.max_price_cents)}¢</span> - ${exit('Close','close',close.enabled,'↑',close.profit_per_ct_cents??0)} - ${exit('Stop','stop',stop.enabled??true,'↓',stop.drop_cents??20)}`;
}
function updatePrimarySummary(){
  const rule={};
  for(const [key,id] of [['minute_from','purchaseMinuteFromInput'],['minute_to','purchaseMinuteToInput'],['contracts','contractsInput'],['min_price_cents','minPriceInput'],['max_price_cents','maxPriceInput']])rule[key]=document.getElementById(id).value;
  Object.assign(rule,readTradeExits(document.getElementById('primaryExitRules')));
  document.querySelector('.primary-details .fallback-header-text').innerHTML=renderTradeHeader('Primary',rule);
}
function updateFallbackSummary(input){
  const row=input.closest('.fallback-rule'),rule={};
  for(const [key] of fallbackFields)rule[key]=row.querySelector(`[data-field="${key}"]`).value;
  Object.assign(rule,readTradeExits(row));
  row.querySelector('.fallback-header-text').innerHTML=renderTradeHeader(row.dataset.tradeName,rule);
}
function updateTradeSummary(input){
  if(input.closest('.fallback-rule'))updateFallbackSummary(input);
  else updatePrimarySummary();
}
function renderFallbackRules(rules){
  const collapsed=Array.from(document.querySelectorAll('#fallbackRules .fallback-details'),el=>!el.open);
  rules=rules.filter(Boolean);
  const addButton='<button class="secondary" type="button" aria-label="Add fallback" title="Add fallback" onclick="addFallbackRule()">+</button>';
  document.getElementById('fallbackRules').innerHTML=rules.map((rule,index)=>`
    <div class="fallback-rule" data-trade-name="FB${index+1}"><details class="fallback-details" ${(rule._collapsed??collapsed[index]??true)?'':'open'}><summary><input data-field="enabled" type="checkbox" aria-label="Enable fallback ${index+1}" ${rule.enabled?'checked':''} onclick="event.stopPropagation()" onchange="orderSettingsDirty=true"><span class="fallback-header-text">${renderTradeHeader("FB"+(index+1),rule)}</span></summary><div class="fallback-content"><div class="purchase-input-row fallback-inputs">
      ${fallbackFields.map(([key,label])=>`<label class="contract-field">${label}<input data-field="${key}" type="number" ${key==='min_distance_dollars'?'':'min="0"'} step="${key==='contracts'?'1':'.01'}" value="${key==='min_distance_dollars'&&rule[key]===null?'':Number(rule[key])}" ${key==='min_distance_dollars'?'placeholder="Off" title="Leave blank to disable the BRTI distance condition"':''} oninput="orderSettingsDirty=true;updateFallbackSummary(this)"></label>`).join('')}
      <label class="contract-field">Changes <input data-field="contract_trend_changes" type="number" min="1" step="1" value="${rule.contract_trend_changes??document.getElementById('contractTrendChangesInput').value}" oninput="orderSettingsDirty=true"></label>
      <label class="contract-field">Strength ≥ <select data-field="min_live_strength" onchange="orderSettingsDirty=true">${['EXTREMELY WEAK','VERY WEAK','WEAK','MODERATE','STRONG'].map(value=>`<option ${(rule.min_live_strength??document.getElementById('minLiveStrengthInput').value)===value?'selected':''}>${value}</option>`).join('')}</select></label>
      <div class="fallback-models" role="group" aria-label="Fallback ${index+1} model agreement filters">
        <span>With Models :</span>
        ${['Pattern','Recent','Bootstrap','Trend'].map(name=>`<label class="contract-field"><input type="checkbox" data-model="${name}" ${(rule.required_models||[]).includes(name)?'checked':''} onchange="orderSettingsDirty=true">${name}</label>`).join('')}
      </div>
    </div>${renderFallbackExit("close_conditions",rule.close_conditions)}${renderFallbackExit("stop_loss",rule.stop_loss)}</div></details><div class="fallback-actions">
      <button class="secondary" type="button" onclick="moveFallbackRule(${index},-1)" ${index===0?'disabled':''}>↑</button>
      <button class="secondary" type="button" onclick="moveFallbackRule(${index},1)" ${index===rules.length-1?'disabled':''}>↓</button>
      <button class="secondary" type="button" aria-label="Remove fallback" title="Remove fallback" onclick="removeFallbackRule(${index})">X</button>
      ${index===rules.length-1?addButton:''}
    </div></div>`).join('')||('<div class="purchase-input-row">'+addButton+'</div>');
}
function addFallbackRule(){
  const rules=readFallbackRules();
  rules.push({enabled:false,minute_from:13,minute_to:15,min_distance_dollars:50,min_price_cents:70,max_price_cents:90,contracts:1});
  renderFallbackRules(rules);orderSettingsDirty=true;
}
function removeFallbackRule(index){
  const rules=readFallbackRules();rules.splice(index,1);renderFallbackRules(rules);orderSettingsDirty=true;
}
function moveFallbackRule(index,delta){
  const rules=readFallbackRules(),other=index+delta;
  if(other<0||other>=rules.length)return;
  [rules[index],rules[other]]=[rules[other],rules[index]];
  renderFallbackRules(rules);orderSettingsDirty=true;
}
async function saveOrderSettings(){
  const status=document.getElementById("orderSettingsStatus");
  const button=document.getElementById("saveOrderSettingsBtn");
  const test_mode=document.getElementById("testModeInput").checked;
  const primary_enabled=document.getElementById("primaryEnabledInput").checked;
  const min_price_cents=Number(document.getElementById("minPriceInput").value);
  const max_price_cents=Number(document.getElementById("maxPriceInput").value);
  const purchase_minute_from=Number(document.getElementById("purchaseMinuteFromInput").value);
  const purchase_minute_to=Number(document.getElementById("purchaseMinuteToInput").value);
  const contracts=Number(document.getElementById("contractsInput").value);
  const entry_stability={enabled:document.getElementById('entryStabilityEnabled').checked,seconds:Number(document.getElementById('entryStabilitySeconds').value)};
  if(!Number.isFinite(entry_stability.seconds)||entry_stability.seconds<=0||entry_stability.seconds>900){status.textContent='Entry stability seconds must be greater than 0 and at most 900.';return;}
  const min_live_strength=document.getElementById("minLiveStrengthInput").value;
  const contract_trend_changes=Number(document.getElementById('contractTrendChangesInput').value);
  if(!Number.isSafeInteger(contract_trend_changes)||contract_trend_changes<1){status.textContent='Changes must be a positive whole number.';return;}
  if(!Number.isFinite(min_price_cents)||min_price_cents<0||min_price_cents>=max_price_cents||!Number.isFinite(max_price_cents)||max_price_cents<=0||max_price_cents>100||!Number.isSafeInteger(contracts)||contracts<1){
    status.textContent="Enter a minimum price of 0 or more, a maximum above it and up to 100 cents, and a positive whole number of contracts.";
    return;
  }
  if(!Number.isFinite(purchase_minute_from)||!Number.isFinite(purchase_minute_to)||purchase_minute_from<0||purchase_minute_from>=purchase_minute_to||purchase_minute_to>15){
    status.textContent="Purchase minutes must satisfy 0 ≤ From < To ≤ 15.";return;
  }
  const required_models=Array.from(document.querySelectorAll('input[name="requiredModel"]:checked'),input=>input.value);
  const agreement_basis=document.getElementById("agreementBasisInput").value;
  const fallbacks=readFallbackRules();
  const accuracy_buy_guard={enabled:document.getElementById('accuracyBuyGuardEnabled').checked,min_accuracy_pct:Number(document.getElementById('accuracyBuyGuardMinInput').value),window_hours:Number(document.getElementById('accuracyBuyGuardHoursInput').value)};
  const choppiness_buy_guard={enabled:document.getElementById('choppinessBuyGuardEnabled').checked,max_score:Number(document.getElementById('choppinessBuyGuardMaxInput').value),rounds:Number(document.getElementById('choppinessBuyGuardRoundsInput').value),source:document.getElementById('choppinessBuyGuardSourceInput').value,metric:'midpoint_crossings',current_enabled:document.getElementById('choppinessCurrentEnabled').checked,current_limit:Number(document.getElementById('choppinessCurrentLimitInput').value),block_next_round:document.getElementById('choppinessBlockNextInput').checked};
  if(!Number.isSafeInteger(choppiness_buy_guard.current_limit)||choppiness_buy_guard.current_limit<1||choppiness_buy_guard.current_limit>59){status.textContent='Current-round crossing limit must be a whole number from 1 to 59.';return;}
  if(document.getElementById('choppinessBuyGuardMaxInput').value.trim()===''||!Number.isFinite(choppiness_buy_guard.max_score)||choppiness_buy_guard.max_score<0||!Number.isSafeInteger(choppiness_buy_guard.rounds)||choppiness_buy_guard.rounds<1||choppiness_buy_guard.rounds>96){status.textContent='Maximum average crossings must be non-negative and completed rounds a whole number from 1 to 96.';return;}
  const allow_multi_buy=document.getElementById('allowMultiBuyInput').checked;
  const time_in_force=document.getElementById('timeInForceInput').value;
  const {stop_loss,close_conditions}=readTradeExits(document.getElementById('primaryExitRules'));
  const force_buy={enabled:document.getElementById('forceBuyEnabled').checked};
  for(const [key,field] of [['minute_from','MinuteFrom'],['minute_to','MinuteTo'],['min_distance_dollars','BrtiDelta'],['min_price_cents','MinPrice'],['max_price_cents','MaxPrice'],['contracts','Contracts']]){
    const value=document.getElementById(`forceBuy${field}Input`).value;
    force_buy[key]=key==='min_distance_dollars'&&value.trim()===''?null:Number(value);
  }
  force_buy.time_in_force=document.getElementById('forceBuyTimeInForceInput').value;
  const force_close={enabled:document.getElementById('forceCloseEnabled').checked,min_price_cents:Number(document.getElementById('forceCloseMinPriceInput').value),max_brti_delta_dollars:document.getElementById('forceCloseBrtiDeltaInput').value.trim()===''?null:Number(document.getElementById('forceCloseBrtiDeltaInput').value),minute_from:Number(document.getElementById('forceCloseMinuteFromInput').value),minute_to:Number(document.getElementById('forceCloseMinuteToInput').value),time_in_force:document.getElementById('forceCloseTimeInForceInput').value};
  button.disabled=true;
  try{
    const response=await fetch("/api/order-settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({require_final_live_agreement:document.getElementById('requireFinalLiveInput').checked,min_model_agreement:Number(document.getElementById('minModelAgreementInput').value),entry_stability,test_mode,choppiness_buy_guard,accuracy_buy_guard,primary_enabled,min_live_strength,stop_loss,force_close,force_buy,contract_trend_changes,time_in_force,allow_multi_buy,close_conditions,fallbacks,purchase_minute_from,purchase_minute_to,min_price_cents,max_price_cents,contracts,required_models,agreement_basis})});
    const data=await response.json();
    if(!response.ok||!data.ok) throw new Error(data.error||"Could not save settings");
    orderSettingsDirty=false;
    status.textContent=`Saved: ${contracts} contracts, price strictly above ${min_price_cents}¢ and below ${max_price_cents}¢; purchase minutes ${purchase_minute_from}–${purchase_minute_to}; models: ${required_models.length?required_models.join(", "):"none"}; agreement: ${agreement_basis}; upward trend over ${contract_trend_changes} price changes. Applies to the next draft.`;
  }catch(error){status.textContent=error.message;}
  finally{button.disabled=false;}
}

async function saveBalance(){
  const button=document.getElementById('saveBalanceBtn'),status=document.getElementById('balanceStatus');
  const value=document.getElementById('initialBalanceInput').value;
  if(value.trim()===''||!Number.isFinite(Number(value))||Number(value)<0){status.textContent='Enter a starting balance of 0 or more.';return;}
  button.disabled=true;
  try{
    const response=await fetch('/api/balance-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initial_balance:Number(value)})});
    const data=await response.json();
    if(!response.ok||!data.ok) throw new Error(data.error||'Could not save balance');
    balanceDirty=false;status.textContent='Starting balance saved.';
  }catch(error){status.textContent=error.message;}
  finally{button.disabled=false;}
}

async function setTarget(){
  const input=document.getElementById("targetInput");
  const btn=document.getElementById("targetBtn");
  const target=parseFloat(input.value);

  if(!Number.isFinite(target)||target<=0){
    alert("Enter a valid Robinhood target.");
    return;
  }

  btn.disabled=true;
  document.getElementById("analysisStatus").textContent="Analyzing historical patterns and backtest…";

  try{
    const r=await fetch("/api/target",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({target})
    });
    const data=await r.json();
    if(!r.ok) throw new Error(data.error||"Failed");
  }catch(e){
    alert(e.message);
    btn.disabled=false;
  }
}

async function reloadHistory(){
  await fetch("/api/reload-history",{method:"POST"});
}

async function setAsset(asset){
  document.getElementById("analysisStatus").textContent=`Switching to ${asset} and loading history…`;
  const r=await fetch("/api/asset",{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({asset})
  });
  const data=await r.json();
  if(!r.ok){ alert(data.error||"Could not change asset"); return; }
  document.getElementById("targetInput").value="";
}

function renderTrackedAccuracy(stats){
  stats=stats||{};
  if(stats.accuracy==null){
    document.getElementById("trackedAccuracy").textContent="—";
    document.getElementById("trackedDetail").textContent=['No settled filled purchases',stats.awaiting_result?`${stats.awaiting_result} awaiting result`:null,stats.unconfirmed_orders?`${stats.unconfirmed_orders} unconfirmed orders`:null].filter(Boolean).join(' · ');
  }else{
    const el=document.getElementById("trackedAccuracy");
    el.textContent=stats.accuracy.toFixed(2)+"%";
    el.className="big "+(stats.accuracy>=50?"green":"red");
    document.getElementById("trackedDetail").textContent=
      [`${stats.total} Trades`,`${stats.hits} HIT / ${stats.misses} MISS`,stats.awaiting_result?`${stats.awaiting_result} awaiting result`:null,stats.breakevens?`${stats.breakevens} break-even`:null].filter(Boolean).join(' · ');
  }

  const pnl=stats.balance?.net_pnl;
  const totalPnl=document.getElementById('trackedTotalPnl');
  totalPnl.textContent=Number.isFinite(pnl)?`$${pnl>=0?'+':''}${pnl.toFixed(4)}`:'—';
  totalPnl.className=pnl>0?'green':pnl<0?'red':'muted';

  const finalStats=stats.final_prediction||{};
  const finalAccuracy=document.getElementById("finalAccuracy");
  finalAccuracy.textContent=finalStats.accuracy==null?"—":finalStats.accuracy.toFixed(2)+"%";
  finalAccuracy.className="big "+(finalStats.accuracy==null?"muted":finalStats.accuracy>=50?"green":"red");
  document.getElementById("finalAccuracyDetail").textContent=finalStats.total
    ?`${finalStats.total} settled rounds · ${finalStats.hits} HIT / ${finalStats.misses} MISS`
    :"No settled predictions";

}

function orderStatusKind(text){
  let kind='info';
  if(/error|failed|could not|timeout|not submitted|browser was closed/i.test(text)) kind='error';
  else if(/skipped|blocked|draft cleared/i.test(text)) kind='skipped';
  else if(/waiting|retrying|refreshing|starting/i.test(text)) kind='waiting';
  else if(/^(filled|submitted|close filled|close submitted|recorded)\b/i.test(text)) kind='success';
  return kind;
}

let orderLogsBusy=false,orderLogsAtLatest=true,orderLogsOldest=null,orderLogsNewest=0;
async function openOrderLogs(){
  document.getElementById('orderLogsDialog').showModal();
  await loadOrderLogs();
}
async function loadOlderOrderLogs(){if(orderLogsOldest)await loadOrderLogs(orderLogsOldest);}
async function loadOrderLogs(before=null){
  if(orderLogsBusy)return;
  orderLogsBusy=true;
  const status=document.getElementById('orderStatusLogs');
  const pageStatus=document.getElementById('orderLogsPageStatus');
  try{
    const response=await fetch('/api/order-status-logs'+(before?`?before=${before}`:''),{cache:'no-store'});
    if(!response.ok)throw new Error('Could not load order logs');
    const data=await response.json();
    const fragment=document.createDocumentFragment();
    for(const entry of data.logs){
      const row=document.createElement('div');row.className='order-message';
      row.dataset.kind=orderStatusKind(entry.message);
      const timestamp=document.createElement('time');timestamp.className='order-status-time';
      timestamp.dateTime=entry.timestamp;timestamp.textContent=new Date(entry.timestamp).toLocaleString();
      row.append(timestamp,document.createTextNode(entry.message));fragment.append(row);
    }
    status.replaceChildren(fragment);status.scrollTop=0;
    orderLogsAtLatest=before===null;
    orderLogsOldest=data.logs.at(-1)?.id??null;
    if(orderLogsAtLatest)orderLogsNewest=data.logs[0]?.id??0;
    document.getElementById('olderOrderLogsBtn').disabled=!data.has_more;
    pageStatus.textContent=data.error||(data.logs.length?`${data.logs.length} messages${orderLogsAtLatest?' · latest':''}`:'No order updates yet.');
  }catch(error){pageStatus.textContent=error.message;}
  finally{orderLogsBusy=false;}
}
function renderOrderStatus(message,logId){
  const text=message||'No order updates yet.';
  const kind=orderStatusKind(text);
  const latest=document.getElementById('robinhoodStatus');
  if(latest.textContent!==text)latest.textContent=text;
  latest.dataset.kind=kind;
  if(document.getElementById('orderLogsDialog').open&&orderLogsAtLatest
      &&logId>0&&logId!==orderLogsNewest&&document.getElementById('orderStatusLogs').scrollTop<32)loadOrderLogs();
  const badge=document.getElementById('orderStatusKind');
  badge.textContent={info:'Info',waiting:'Waiting',skipped:'Skipped',success:'Success',error:'Error'}[kind];
  badge.style.color={info:'var(--blue)',waiting:'var(--yellow)',skipped:'var(--muted)',success:'var(--green)',error:'var(--red)'}[kind];
}

function syncOrderSettings(s){
  const settingsFocused=!!document.activeElement?.closest('#orderSettingsPanel, #closeSettingsPanel');
  const settingsKey=JSON.stringify(s.order_settings);
  if(s.order_settings&&!settingsFocused&&(!orderSettingsLoaded||!orderSettingsDirty)
      &&(!orderSettingsLoaded||settingsKey!==renderedOrderSettings)){
    document.getElementById("testModeInput").checked=s.order_settings.test_mode===true;
    document.getElementById("primaryEnabledInput").checked=s.order_settings.primary_enabled!==false;
    document.getElementById("purchaseMinuteFromInput").value=s.order_settings.purchase_minute_from??0;
    document.getElementById("purchaseMinuteToInput").value=s.order_settings.purchase_minute_to??15;
    document.getElementById("minPriceInput").value=s.order_settings.min_price_cents??0;
    document.getElementById("maxPriceInput").value=s.order_settings.max_price_cents;
    document.getElementById("contractsInput").value=s.order_settings.contracts;
    document.getElementById('primaryExitRules').innerHTML=renderFallbackExit('close_conditions',s.order_settings.close_conditions)+renderFallbackExit('stop_loss',s.order_settings.stop_loss);
    updatePrimarySummary();
    document.getElementById('entryStabilityEnabled').checked=s.order_settings.entry_stability?.enabled??false;
    document.getElementById('entryStabilitySeconds').value=s.order_settings.entry_stability?.seconds??60;
    document.getElementById("minLiveStrengthInput").value=s.order_settings.min_live_strength||"MODERATE";
    document.getElementById("contractTrendChangesInput").value=s.order_settings.contract_trend_changes??10;
    document.getElementById("agreementBasisInput").value=s.order_settings.agreement_basis||"either";
    document.getElementById("requireFinalLiveInput").checked=!!s.order_settings.require_final_live_agreement;
    document.getElementById("minModelAgreementInput").value=String(s.order_settings.min_model_agreement??0);
    const required=s.order_settings.required_models||["Pattern","Recent","Bootstrap","Trend"];
    document.querySelectorAll('input[name="requiredModel"]').forEach(input=>{input.checked=required.includes(input.value);});
    renderFallbackRules((s.order_settings.fallbacks||[s.order_settings.fallback]).filter(Boolean).map(rule=>({...rule,close_conditions:rule.close_conditions||s.order_settings.close_conditions,stop_loss:rule.stop_loss||s.order_settings.stop_loss})));
    document.getElementById('accuracyBuyGuardEnabled').checked=!!s.order_settings.accuracy_buy_guard?.enabled;
    document.getElementById('accuracyBuyGuardMinInput').value=s.order_settings.accuracy_buy_guard?.min_accuracy_pct??60;
    document.getElementById('accuracyBuyGuardHoursInput').value=s.order_settings.accuracy_buy_guard?.window_hours??24;
    document.getElementById('choppinessBuyGuardEnabled').checked=!!s.order_settings.choppiness_buy_guard?.enabled;
    document.getElementById('choppinessBuyGuardMaxInput').value=s.order_settings.choppiness_buy_guard?.max_score??3;
    document.getElementById('choppinessBuyGuardRoundsInput').value=s.order_settings.choppiness_buy_guard?.rounds??3;
    document.getElementById('choppinessBuyGuardSourceInput').value=s.order_settings.choppiness_buy_guard?.source??'bitcoin';
    document.getElementById('choppinessCurrentEnabled').checked=!!s.order_settings.choppiness_buy_guard?.current_enabled;
    document.getElementById('choppinessCurrentLimitInput').value=s.order_settings.choppiness_buy_guard?.current_limit??7;
    document.getElementById('choppinessBlockNextInput').checked=!!s.order_settings.choppiness_buy_guard?.block_next_round;
    document.getElementById('allowMultiBuyInput').checked=s.order_settings.allow_multi_buy!==false;
    document.getElementById('timeInForceInput').value=s.order_settings.time_in_force||'ioc';
    const forceBuy=s.order_settings.force_buy||{enabled:false,minute_from:0,minute_to:15,min_distance_dollars:null,min_price_cents:0,max_price_cents:100,contracts:1,time_in_force:'ioc'};
    document.getElementById('forceBuyEnabled').checked=forceBuy.enabled;
    for(const [key,field] of [['minute_from','MinuteFrom'],['minute_to','MinuteTo'],['min_distance_dollars','BrtiDelta'],['min_price_cents','MinPrice'],['max_price_cents','MaxPrice'],['contracts','Contracts']])document.getElementById(`forceBuy${field}Input`).value=forceBuy[key]??'';
    document.getElementById('forceBuyTimeInForceInput').value=forceBuy.time_in_force;
    const manual=s.order_settings.force_close||{enabled:false,min_price_cents:0,minute_from:0,minute_to:15,time_in_force:'ioc'};
    document.getElementById('forceCloseEnabled').checked=manual.enabled;
    document.getElementById('forceCloseMinPriceInput').value=manual.min_price_cents;
    document.getElementById('forceCloseBrtiDeltaInput').value=manual.max_brti_delta_dollars??'';
    document.getElementById('forceCloseMinuteFromInput').value=manual.minute_from;
    document.getElementById('forceCloseMinuteToInput').value=manual.minute_to;
    document.getElementById('forceCloseTimeInForceInput').value=manual.time_in_force;
    orderSettingsLoaded=true;
    renderedOrderSettings=settingsKey;
  }
}

let forceBuyContext=null,forceBuyBusy=false;
async function requestForceBuy(){
  if(forceBuyBusy||!forceBuyContext)return;
  forceBuyBusy=true;
  const button=document.getElementById('forceBuyBtn');button.disabled=true;
  try{
    const response=await fetch('/api/force-buy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(forceBuyContext)});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'Force Buy request failed');
    button.textContent='Force Buy pending';
  }catch(error){document.getElementById('orderSettingsStatus').textContent=error.message;}
  finally{forceBuyBusy=false;await poll();}
}

function renderChoppinessGuard(guard){
  const status=document.getElementById('choppinessBuyGuardStatus');
  if(!guard){status.textContent='';status.title='';return;}
  const score=value=>Number.isFinite(value)?value.toFixed(2):'—';
  const bitcoin=guard.source==='bitcoin';
  const source=bitcoin?'Bitcoin':'Contract Yes';
  const range=(item,average=false)=>{
    const value=item?.[`${average?'average_range':'range'}_${bitcoin?'dollars':'cents'}`];
    return Number.isFinite(value)?bitcoin?`$${score(value)}`:`${score(value)}¢`:'—';
  };
  const current=guard.current_round||{};
  const count=current.observed_crossings??current.score;
  const currentText=`Current crossings (partial): ${Number.isFinite(count)?count:'—'} · Range: ${range(current)}`;
  const averageText=`Last ${guard.required_rounds} avg: ${score(guard.average_score)} · Avg range: ${range(guard,true)} (${guard.ready_rounds}/${guard.required_rounds} ready)`;
  const until=guard.blocked_until?new Date(guard.blocked_until).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'round end';
  const state=!guard.enabled?'Filter off':guard.current_blocked?`Buying paused until ${until}: current round ≥ ${guard.current_limit}`:guard.previous_blocked?`Buying paused until ${until}: previous round reached ${guard.previous_crossings}`:guard.allowed?'Buying allowed':!Number.isFinite(guard.average_score)?'Buying paused: insufficient data':'Buying paused: average crossings above limit';
  const gates=`Average filter: ${guard.average_enabled?'on':'off'} · Current gate: ${guard.current_enabled?'on at ≥ '+guard.current_limit:'off'} · Following-round gate: ${guard.block_next_round?'on':'off'}`;
  status.textContent=`${source} · ${currentText} · ${averageText} · ${gates} · ${state}`;
  status.style.color=!guard.enabled?'':guard.allowed?'#62daa3':'#ff727d';
  status.title=(guard.round_scores||[]).map(round=>`${source} · ${new Date(round.round_start).toLocaleString()}: ${score(round.score)} crossings · Range ${range(round)} · Samples ${round.sample_count??0}/${round.expected_samples??0}${round.missing_samples?' · gaps ignored':''}${round.status==='ready'?'':' · insufficient data'}`).join('\n');
}

function render(s){
  syncHeaderCountdown(s);
  forceBuyContext={asset:s.asset,round_start:s.round?.start};
  const forceBuyButton=document.getElementById('forceBuyBtn');
  forceBuyButton.disabled=forceBuyBusy||!s.order_settings?.force_buy?.enabled||s.force_buy_pending||!s.prediction;
  forceBuyButton.textContent=s.force_buy_pending?'Force Buy pending':'Force Buy';
  if(s.order_settings&&!balanceDirty&&document.activeElement?.id!=="initialBalanceInput") document.getElementById("initialBalanceInput").value=s.order_settings.initial_balance||0;
  syncOrderSettings(s);
  renderChoppinessGuard(s.stats?.buy_choppiness_guard);
  const accuracyGuard=s.stats?.buy_accuracy_guard;
  const accuracyGuardStatus=document.getElementById('accuracyBuyGuardStatus');
  if(accuracyGuard){
    accuracyGuardStatus.textContent=`Last ${accuracyGuard.window_hours??24}h: ${accuracyGuard.total?accuracyGuard.accuracy.toFixed(2)+'% ('+accuracyGuard.hits+'/'+accuracyGuard.total+')':'No settled predictions'} · ${!accuracyGuard.enabled?'Filter off':accuracyGuard.allowed?'Buying allowed':'Automatic test mode · Real buys paused'}`;
    accuracyGuardStatus.style.color=!accuracyGuard.enabled?'':accuracyGuard.allowed?'#62daa3':'#f5c76b';
  }
  const balance=s.stats?.balance||{};
  const available=s.available_balance;
  document.getElementById("currentBalance").textContent=available?fmtMoney(available.amount):'—';
  const initialBalance=s.order_settings?.initial_balance;
  const balancePnl=Number.isFinite(available?.amount)&&Number.isFinite(initialBalance)?Math.round((available.amount-initialBalance)*100)/100:null;
  document.getElementById("netPnl").textContent=balancePnl==null?'—':fmtSigned(balancePnl);
  document.getElementById("netPnl").className=balancePnl==null?'muted':clsSigned(balancePnl);
  document.getElementById("netPnl").title='Current available balance minus saved starting balance';
  document.getElementById('balanceCompactValue').textContent=document.getElementById('currentBalance').textContent;
  document.getElementById('balanceCompactPnl').textContent=document.getElementById('netPnl').textContent;
  document.getElementById('balanceCompactPnl').className=document.getElementById('netPnl').className;
  document.getElementById("balanceDetail").textContent=`${balance.counted_purchases||0} settled purchases · ${balance.pending_or_missing||0} pending / missing data · all rounds · estimated fee 1.94¢ / CT on buy; 1.94¢ on filled sell; settlement: $1 winner / $0 loser, no sell fee`;
  const asset=s.asset||"BTC";
  document.getElementById("assetTitle").textContent=asset;
  const stability=s.entry_stability;
  document.getElementById('entryStabilityStatus').textContent=s.order_settings?.entry_stability?.enabled
    ?`Observed: ${stability?.direction||'waiting for fresh BTC quotes'} · ${(stability?.seconds||0).toFixed(1)} / ${s.order_settings.entry_stability.seconds}s`:'';
  renderMarketCharts(s);
  document.getElementById("assetSelect").value=asset;
  const contractPrices=s.contract_prices;
  document.getElementById("contractYesPrice").textContent=contractPrices?`${contractPrices.yes_cents}¢`:"—";
  document.getElementById("contractNoPrice").textContent=contractPrices?`${contractPrices.no_cents}¢`:"—";
  document.getElementById("contractPriceTime").textContent=contractPrices?`🕒 ${new Date(contractPrices.updated_at).toLocaleTimeString()}`:'Waiting for current contract quote';
  applyMarketLineVisibility();
  renderOrderStatus(s.robinhood_status,s.order_status_log_id);
  const confirmClosed=document.getElementById('confirmClosedStatus');
  confirmClosed.hidden=!s.manual_close;
  confirmClosed.closePayload=s.manual_close;
  const round=s.round||{};
  document.getElementById("roundBadge").textContent=
    `${round.start_label||"--:--"} → ${round.end_label||"--:--"} · ${secondsText(round.seconds_left)}`;
  document.getElementById("roundText").textContent=
    `${round.start_label||"--:--"} → ${round.end_label||"--:--"}`;
  document.getElementById("timer").textContent=`Time remaining: ${secondsText(round.seconds_left)}`;

  if(lastRoundStart&&round.start!==lastRoundStart){
    document.getElementById("targetInput").value="";
  }
  lastRoundStart=round.start;

  const live=s.live||{};
  const shownPrice=live.price??live.last_observed_price;
  const priceElement=document.getElementById('livePrice');
  priceElement.textContent=fmtMoney(shownPrice);
  priceElement.className=live.stale?'big yellow':'big';
  priceElement.title=`${live.source||''}${live.updated_at?' · '+new Date(live.updated_at).toLocaleTimeString():''}${live.stale?' · Last observed price; stale, buying blocked':''}`;
  const ageLabel=document.getElementById('livePriceFreshness');
  ageLabel.textContent=Number.isFinite(live.source_age_seconds)?`${live.source_age_seconds.toFixed(1)}s`:'';
  ageLabel.className=live.stale?'red':'muted';
  ageLabel.title='Age since the source published this price';
  const ld=document.getElementById('liveDelta');
  if(typeof shownPrice==='number'&&typeof s.target==='number'&&s.target>0){
    const delta=shownPrice-s.target;
    ld.className=live.stale?'muted':clsSigned(delta);
    ld.textContent=`${fmtSigned(delta)} (${fmtSigned(delta/s.target*100,4)}%)`;
  }else{
    ld.className='muted';ld.textContent='';
  }

  document.getElementById("targetDisplay").textContent=
    s.target==null?"Not entered":fmtMoney(s.target);

  const status=document.getElementById("analysisStatus");
  const btn=document.getElementById("targetBtn");
  btn.disabled=s.analysis_status==="analyzing"||(s.history&&s.history.loading);

  if(s.analysis_status==="analyzing"){
    status.textContent="Analyzing… this can take a few seconds.";
  }else if(s.analysis_status==="error"){
    status.textContent="Error: "+(s.analysis_error||"Unknown error");
  }else if(s.analysis_status==="ready"){
    status.textContent="Prediction frozen for this round.";
  }else{
    status.textContent=(s.history&&s.history.loading)
      ?"Loading/refreshing historical data…"
      :"Enter the Robinhood target to generate the frozen prediction.";
  }

  function appendRoundLearning(box,info){
    if(!info)return;
    const detail=document.createElement('div');
    detail.className='muted';detail.style.fontSize='11px';detail.style.marginTop='8px';
    const status=info.applied?'active':info.status==='shadow'?'evaluating':info.status==='training'?'training':'collecting data';
    detail.textContent=`Round learning: ${status} · ${info.usable_rounds??0} settled rounds`;
    if(info.candidate&&info.baseline)detail.textContent+=` · holdout accuracy ${(100*info.baseline.accuracy).toFixed(1)}% → ${(100*info.candidate.accuracy).toFixed(1)}%`;
    box.append(detail);
  }
  const p=s.prediction;
  const box=document.getElementById("decisionBox");

  if(p){
    const up=p.prediction.includes("UP");
    box.className="direction "+(up?"up":"down");
    box.innerHTML=`
      <div class="decision ${up?"green":"red"}">${p.prediction}</div>
      <div class="medium">${p.confidence.toFixed(2)}% · ${p.strength}</div>
      <div class="probbar"><div style="width:${p.p_up}%"></div></div>
      <div class="spread" style="margin-top:8px">
        <span class="green">ABOVE ${p.p_up.toFixed(2)}%</span>
        <span class="red">BELOW ${p.p_down.toFixed(2)}%</span>
      </div>`;

    appendRoundLearning(box,p.learning);
    document.getElementById("modelsBody").innerHTML=p.models.map(m=>{
      const c=m.probability_up>=55?"green":m.probability_up<=45?"red":"yellow";
      return `<tr>
        <td>${m.label}</td>
        <td class="${c}"><b>${m.probability_up.toFixed(2)}%</b></td>
        <td>${m.weight.toFixed(2)}%</td>
        <td>${m.bt_accuracy.toFixed(2)}%</td>
      </tr>`;
    }).join("");

    const bt=p.backtest;
    const bta=document.getElementById("btAccuracy");
    bta.textContent=bt.accuracy.toFixed(2)+"%";
    bta.className="big "+(bt.accuracy>=50?"green":"red");
    document.getElementById("btDetails").innerHTML=
      `${bt.rounds} rounds · ${bt.hits}H/${bt.misses}M · `+
      `Brier: ${bt.brier==null?"—":bt.brier.toFixed(4)} · Calibration: ${bt.calibration_samples}`;

    document.getElementById("moves").innerHTML=
      `P10 ${fmtSigned(p.moves.p10,4)}% · `+
      `Median ${fmtSigned(p.moves.median,4)}% · `+
      `P90 ${fmtSigned(p.moves.p90,4)}%`;
  }else{
    box.className="direction";
    box.innerHTML='<div class="decision muted">WAITING FOR TARGET</div>';
    document.getElementById("modelsBody").innerHTML='<tr><td colspan="4" class="muted">Waiting for prediction…</td></tr>';
    document.getElementById("btAccuracy").textContent="—";
    document.getElementById("btDetails").textContent="—";
    document.getElementById("moves").textContent="—";
  }

  const rec=s.live_recommendation;
  const recBox=document.getElementById("liveRecommendation");
  if(rec){
    const up=rec.recommendation.includes("UP");
    recBox.className="direction "+(up?"up":"down");
    recBox.innerHTML=`
      <div class="decision ${up?"green":"red"}">${rec.recommendation}</div>
      <div class="medium">${rec.confidence.toFixed(2)}% · ${rec.strength}</div>
      <div class="muted" style="margin-top:8px">
        Minute ${String(rec.minute).padStart(2,"0")} · ${rec.remaining_minutes} min left<br>
        Since start: <span class="${clsSigned(rec.change_dollars)}">${fmtSigned(rec.change_dollars)} (${fmtSigned(rec.change_pct,4)}%)</span>
      </div>`;
    appendRoundLearning(recBox,rec.learning);
    const support=rec.market_support;
    if(support){
      const detail=document.createElement('div');
      detail.className='muted';detail.style.fontSize='11px';
      const neutralReason={weak_relationship:'weak relationship',stale_btc:'stale BTC price',stale_contract:'stale contract quote',no_clear_move:'no clear movement',insufficient_data:'insufficient data'}[support.reason]||'insufficient fresh evidence';
      detail.textContent=support.applied?`Market support (60s): ${support.reason==='aligned'?'Aligned':'Opposed'} · ${Math.abs(support.adjustment*100).toFixed(2)} pp · R² ${(support.r2*100).toFixed(0)}%`:`Market support (60s): neutral · ${neutralReason}`;
      recBox.append(detail);
    }
  }else{
    recBox.className="direction";
    const reason=s.analysis_error?'Live data unavailable':s.target==null?'Waiting for target':s.analysis_status==='analyzing'?'Calculating round prediction':!s.prediction?'Waiting for round prediction':'Waiting for fresh price';
    recBox.innerHTML='<div class="decision muted">WAITING</div>';
    const detail=document.createElement('div');
    detail.className='muted';
    detail.style.fontSize='11px';
    detail.textContent=reason;
    recBox.append(detail);
  }

  const quotes=s.quotes||{};
  const h=s.history||{};
  document.getElementById("historyStatus").textContent=
    "History: "+(h.message||"—");

  document.getElementById("warningText").textContent=s.warning||"";


}

let roundHistoryPage=1, roundHistoryAsset=null, roundHistoryBusy=false;
const escapeHistory=v=>String(v??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function historyDirection(direction){
  if(!direction) return '<span class="round-empty">No prediction</span>';
  const up=String(direction).startsWith('UP');
  return `<span class="round-tag ${up?'green':'red'}" title="${up?'UP / ABOVE':'DOWN / BELOW'}" aria-label="${up?'UP / ABOVE':'DOWN / BELOW'}">${up?'↑':'↓'}</span>`;
}
function historyDecision(direction, probability, hit){
  if(!direction) return '<span class="round-empty">No prediction</span>';
  const up=String(direction).startsWith('UP');
  const chance=probability==null?null:(up?Number(probability):1-Number(probability))*100;
  const pct=chance==null?'':`<span class="round-prob" title="Probability of this direction">🎯 ${chance.toFixed(1)}%</span>`;
  const result=hit==null?' ⋯':` ${hit?'✓':'✕'}`;
  const description=`${up?'UP / ABOVE':'DOWN / BELOW'} · ${hit==null?'Result pending':hit?'HIT':'MISS'}`;
  const tag=`<span class="round-tag ${up?'green':'red'}" title="${description}" aria-label="${description}">${up?'↑':'↓'}<span class="${hit==null?'muted':hit?'green':'red'}">${result}</span></span>`;
  return tag+pct;
}
function historyStatus(status){
  return ({settled:'Settled',predicted:'Prediction ready',waiting_target:'Waiting for target',analyzing:'Analyzing',unsettled:'No settlement samples',no_prediction:'No prediction',error:'Analysis failed'})[status]||status;
}


function settlementHistoryCell(row){
  if(!row.actual&&row.settlement==null) return '<span class="round-empty">—</span>';
  const direction=row.actual?historyDirection(row.actual):'<span class="round-empty">—</span>';
  const source=row.settlement_source==='next_round_target'?'Settlement uses the next consecutive round target':'Recorded settlement price';
  const confirmed=row.settlement_source!=='next_round_target'&&row.result_confirmed_by==='user'?'<span class="round-prob" title="Outcome confirmed by user">User confirmed</span>':'';
  return `${direction}<span class="round-prob"><strong title="${source}">${fmtMoney(row.settlement)}</strong></span>${settlementDifferenceCell(row)}${confirmed}`;
}
function settlementDifferenceCell(row){
  if(typeof row.settlement!=='number'||typeof row.target!=='number'||!Number.isFinite(row.settlement)||!Number.isFinite(row.target)) return '';
  const difference=row.settlement-row.target;
  return `<span class="round-prob ${clsSigned(difference)}" title="Settlement minus target">Δ $${fmtSigned(difference)}</span>`;
}
const marketLineVisibility={bitcoin:true,contract:true};
try{
  marketLineVisibility.bitcoin=localStorage.getItem('dashboard.showBitcoinLine')!=='false';
  marketLineVisibility.contract=localStorage.getItem('dashboard.showContractLine')!=='false';
}catch{}
function setMarketLineVisibility(){
  marketLineVisibility.bitcoin=document.getElementById('showBitcoinLine').checked;
  marketLineVisibility.contract=document.getElementById('showContractLine').checked;
  try{
    localStorage.setItem('dashboard.showBitcoinLine',String(marketLineVisibility.bitcoin));
    localStorage.setItem('dashboard.showContractLine',String(marketLineVisibility.contract));
  }catch{}
  applyMarketLineVisibility();
}
function applyMarketLineVisibility(){
  document.getElementById('showBitcoinLine').checked=marketLineVisibility.bitcoin;
  document.getElementById('showContractLine').checked=marketLineVisibility.contract;
  document.querySelectorAll('#underlyingPriceChart .underlying-price-line,#underlyingPriceChart .underlying-price-endpoint,#underlyingPriceChart .contract-cursor circle').forEach(node=>node.style.display=marketLineVisibility.bitcoin?'':'none');
  document.querySelectorAll('#underlyingPriceChart .merged-contract-line').forEach(node=>node.style.display=marketLineVisibility.contract?'':'none');
}

function renderMarketCharts(s){
  const asset=s.asset||'BTC';
  document.getElementById('underlyingChartTitle').textContent=`${asset==='BTC'?'Bitcoin':asset} + Yes during this round`;
  const chart=document.getElementById('underlyingPriceChart');
  const chartKey=JSON.stringify([asset,s.round?.start??s.underlying_price_history?.[0]?.observed_at??null]);
  const oldSvg=chart.querySelector('svg[data-unit="usd"]');
  const sameRound=chart.dataset.roundKey===chartKey;
  const selected=sameRound&&oldSvg?.querySelector('.contract-cursor')?.getAttribute('visibility')==='visible'
    ?oldSvg.querySelectorAll('.contract-observation')[Number(oldSvg.dataset.index)]?.dataset.observedAt:null;
  const focused=sameRound&&document.activeElement===oldSvg;
  const observations=s.underlying_price_history||[],contractQuotes=s.contract_price_history||[];
  const tail=observations.at(-1),quoteTail=contractQuotes.at(-1);
  const renderKey=JSON.stringify([chartKey,s.target,observations.length,tail?.observed_at,tail?.price,contractQuotes.length,quoteTail,s.live_recommendation_changes,s.chart_trades]);
  const chartChanged=chart.dataset.renderKey!==renderKey;
  if(chartChanged){
    chart.innerHTML=underlyingPriceChart(observations,s.target,contractQuotes,s.live_recommendation_changes,s.chart_trades);
    chart.dataset.renderKey=renderKey;
  }
  chart.dataset.roundKey=chartKey;
  const svg=chart.querySelector('svg[data-unit="usd"]');
  if(chartChanged&&selected&&svg){
    const index=Array.from(svg.querySelectorAll('.contract-observation')).findIndex(point=>point.dataset.observedAt===selected);
    if(index>=0)showContractChartPoint(svg,index);
  }
  if(focused&&svg)svg.focus({preventScroll:true});
  const last=s.underlying_price_history?.at(-1),v=last?.volatility_60s;
  const age=last?(Date.parse(s.server_time)-Date.parse(last.observed_at))/1000:Infinity;
  const colored=(value,color)=>`<strong class="volatility-value" style="color:${color};font-variant-numeric:tabular-nums">${escapeHistory(value)}</strong>`;
  document.getElementById('bitcoinVolatility').innerHTML=v?.status==='ready'
    ?`Range: ${colored(fmtMoney(v.range_dollars),'var(--yellow)')} (${colored(v.range_pct.toFixed(3)+'%','var(--yellow)')}) · Reversals: ${colored(v.direction_reversals??'—','var(--blue)')} (↑ ${colored(v.up_moves??'—','var(--green)')} / ↓ ${colored(v.down_moves??'—','var(--red)')}) · ↑ ${colored(fmtMoney(v.high),'var(--green)')} / ↓ ${colored(fmtMoney(v.low),'var(--red)')}${age>20?' · <strong style="color:var(--red)">Stale observation</strong>':''}`
    :'Range: insufficient samples (collecting this round)';
  const latestPoint=Array.from(svg?.querySelectorAll('.contract-observation')||[]).at(-1);
  const sensitivity=latestPoint?.dataset.dollarsPerCent;
  const r2=latestPoint?.dataset.sensitivityR2;
  const sensitivityText=sensitivity&&age<=10
    ?`BTC / 1¢ ≈ ${colored(fmtMoney(sensitivity),'var(--cyan)')} · R² ${colored((Number(r2)*100).toFixed(0)+'%','var(--cyan)')}`
    :'BTC / 1¢: unavailable';
  document.getElementById('bitcoinVolatility').innerHTML+=' · '+sensitivityText;
  applyMarketLineVisibility();
}

function volatilityChartIsHidden(key){
  if(Object.hasOwn(volatilityChartIsHidden.cache??{},key))return volatilityChartIsHidden.cache[key];
  try{return localStorage.getItem(`dashboard.volatility.${key}.hidden`)==='true';}catch{return false;}
}
function saveVolatilityChartVisibility(panel){
  const key=panel.dataset.volatilityChart,hidden=!panel.open;
  volatilityChartIsHidden.cache??={};
  volatilityChartIsHidden.cache[key]=hidden;
  try{localStorage.setItem(`dashboard.volatility.${key}.hidden`,String(hidden));}catch{}
}

function volatilityHistoryChart(samples){
  const points=(samples||[]).filter(p=>p.volatility_60s?.status==='ready'&&Number.isFinite(p.volatility_60s.range_dollars));
  if(!points.length)return '<div class="muted">60s range history: no complete recorded windows yet.</div>';
  return [
    ['range_dollars','Last 60s observed high − low ($), during this round','Recorded rolling 60 second price range','var(--yellow)'],
    ['direction_reversals','Direction reversals in last 60s (observed samples)','Recorded rolling 60 second direction reversals','var(--blue)']
  ].map(([key,label,aria,color])=>{
    const series=points.filter(p=>Number.isFinite(p.volatility_60s[key]));
    if(!series.length)return '';
    const ceiling=Math.max(1,...series.map(p=>p.volatility_60s[key]));
    const x=m=>96+m/15*836,y=v=>28+(1-v/ceiling)*75;
    const path=series.map((p,i)=>`${!i||p.minute-series[i-1].minute>1/3?'M':'L'} ${x(p.minute)} ${y(p.volatility_60s[key])}`).join(' ');
    return `<details class="volatility-panel" data-volatility-chart="${key}" ${volatilityChartIsHidden(key)?'':'open'} ontoggle="saveVolatilityChartVisibility(this)"><summary class="muted" style="cursor:pointer;margin-top:8px">${label}</summary><svg viewBox="0 0 1000 130" role="img" aria-label="${aria}"><text x="88" y="32" text-anchor="end" fill="var(--muted)">${key==='direction_reversals'?ceiling:ceiling.toFixed(2)}</text><text x="88" y="107" text-anchor="end" fill="var(--muted)">0</text><path d="${path}" fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>${[0,3,6,9,12,15].map(m=>`<text x="${x(m)}" y="124" text-anchor="middle" fill="var(--muted)">${m}</text>`).join('')}</svg></details>`;
  }).join('');
}

function bitcoinDollarsPerCent60s(points,quotes){
  // Pair each contract observation with the most recent BTC sample, at most 5s old.
  // Fit BTC dollars against Yes cents within the preceding 60s; never use future data.
  const time=p=>Number.isFinite(Date.parse(p.observed_at))?Date.parse(p.observed_at)/1000:p.minute*60;
  const prices=points.map(p=>({t:time(p),price:p.price})).sort((a,b)=>a.t-b.t);
  const pairs=[];
  let index=0;
  for(const quote of [...quotes].sort((a,b)=>time(a)-time(b))){
    const t=time(quote);
    while(index+1<prices.length&&prices[index+1].t<=t)index++;
    const btc=prices[index];
    if(btc&&btc.t<=t&&t-btc.t<=5&&Number.isFinite(quote.yes_cents)){
      if(pairs.at(-1)?.t===t)continue;
      pairs.push({t,x:quote.yes_cents,y:btc.price});
    }
  }
  const results=new Array(points.length).fill(null);
  const origin=pairs[0]?.y||0;
  let left=0,right=0,sx=0,sy=0,sxx=0,syy=0,sxy=0;
  const accumulate=(p,sign)=>{
    const y=p.y-origin;
    sx+=sign*p.x;sy+=sign*y;sxx+=sign*p.x*p.x;syy+=sign*y*y;sxy+=sign*p.x*y;
  };
  const ordered=points.map((point,index)=>({point,index,t:time(point)})).sort((a,b)=>a.t-b.t);
  for(const {point,index,t} of ordered){
    while(right<pairs.length&&pairs[right].t<=t)accumulate(pairs[right++],1);
    while(left<right&&pairs[left].t<t-60)accumulate(pairs[left++],-1);
    const n=right-left;
    if(point.minute<1||n<5||t-pairs[right-1].t>10||pairs[right-1].t-pairs[left].t<45)continue;
    const xx=sxx-sx*sx/n,yy=syy-sy*sy/n,xy=sxy-sx*sy/n;
    if(xx<=0||yy<=0||xy<=0)continue;
    let lo=Infinity,hi=-Infinity;
    for(let i=left;i<right;i++){lo=Math.min(lo,pairs[i].x);hi=Math.max(hi,pairs[i].x);}
    if(hi-lo<1)continue;
    const r2=Math.min(1,xy*xy/(xx*yy));
    if(r2>=.5)results[index]={dollars:xy/xx,r2,samples:n};
  }
  return results;
}

function chartTradeEvents(row){
  if(!row)return [];
  const start=Date.parse(row.round_start),events=[],seen=new Set();
  for(const trade of [...(row.completed_purchases||[]),row.purchase].filter(Boolean)){
    if(trade.status!=='filled')continue;
    const settled=['UP / ABOVE','DOWN / BELOW'].includes(row.actual);
    const settlementClose=settled&&trade.close_order?.status!=='filled'
      ?{status:'filled',filled_at:row.round_end,execution_price_cents:trade.direction===row.actual?100:0,reason:'Settlement'}:null;
    for(const [kind,order] of [['Buy',trade],['Close',settlementClose||trade.close_order]]){
      if(!order||order.status!=='filled')continue;
      const stamp=order.filled_at||order.submitted_at||order.attempted_at;
      const minute=(Date.parse(stamp)-start)/60000;
      if(!Number.isFinite(minute)||minute<0||minute>15)continue;
      const key=JSON.stringify([kind,stamp,trade.attempted_at,trade.submitted_at,trade.direction]);
      if(seen.has(key))continue;
      seen.add(key);
      events.push({kind,minute,stamp,direction:trade.direction,test:trade.test_mode===true,
        price:order.execution_price_cents??order.limit_price_cents,
        estimated:order.execution_price_cents==null,reason:order.reason});
    }
  }
  return events.sort((a,b)=>a.minute-b.minute);
}

function underlyingPriceChart(samples,target,contractSamples=null,liveChanges=[],tradeRow=null){
  const points=(samples||[]).filter(p=>Number.isFinite(p.price)&&Number.isFinite(p.minute)&&p.minute>=0&&p.minute<15);
  const hasTarget=typeof target==='number'&&Number.isFinite(target);
  if(!points.length&&!hasTarget) return '<div class="muted" style="padding:12px 0">Waiting for fresh price observations and target…</div>';
  const merged=contractSamples!==null;
  const quotes=(contractSamples||[]).filter(p=>Number.isFinite(p.minute)&&p.minute>=0&&p.minute<15&&Number.isFinite(p.yes_cents)&&Number.isFinite(p.no_cents));
  const dollarsPerCent=bitcoinDollarsPerCent60s(points,quotes);
  const w=1000,h=472,l=96,r=merged?68:24,t=140,b=54;
  const values=points.map(p=>p.price);if(hasTarget)values.push(target);
  const min=Math.min(...values),max=Math.max(...values),pad=Math.max((max-min)*.12,5);
  const range=hasTarget?Math.max(...values.map(value=>Math.abs(value-target)))+pad:0;
  const deltaStep=Math.max(25,25*Math.ceil(range/4/25));
  const halfRange=hasTarget?deltaStep*Math.max(1,Math.ceil(range/deltaStep)):0;
  const low=hasTarget?target-halfRange:min-pad,high=hasTarget?target+halfRange:max+pad;
  const x=minute=>l+minute/15*(w-l-r),y=price=>t+(high-price)/(high-low)*(h-t-b);
  let axes='';
  const ticks=hasTarget?Array.from({length:Math.round(halfRange/deltaStep)*2+1},(_,i)=>low+i*deltaStep):Array.from({length:6},(_,i)=>low+(high-low)*i/5);
  for(const value of ticks){
    const py=y(value),delta=hasTarget?Math.round(value-target):null;
    const label=hasTarget?(delta===0?'0':`${delta>0?'+':'−'}${Math.abs(delta).toLocaleString()}`):fmtMoney(value);
    axes+=`<line x1="${l}" y1="${py}" x2="${w-r}" y2="${py}" stroke="var(--line)" vector-effect="non-scaling-stroke"/><text class="underlying-axis-label" data-delta="${delta??''}" x="${l-8}" y="${py+5}" text-anchor="end">${label}</text>`;
  }
  for(const minute of [0,3,6,9,12,15])axes+=`<text x="${x(minute)}" y="${h-b+23}" text-anchor="middle">${minute}</text>`;
  axes+=`<text x="${l+(w-l-r)/2}" y="${h-6}" text-anchor="middle">Minutes after round start</text>`;
  const targetLine=hasTarget?`<g class="underlying-target"><line x1="${l}" y1="${y(target)}" x2="${w-r}" y2="${y(target)}" stroke="var(--yellow)" stroke-width="1.5" stroke-dasharray="6 5" vector-effect="non-scaling-stroke"/><text x="${w-r}" y="${y(target)-8}" text-anchor="end" fill="var(--yellow)" font-size="15">Target ${fmtMoney(target)}</text></g>`:'';
  const path=points.map((p,i)=>`${!i||p.minute-points[i-1].minute>.25?'M':'L'} ${x(p.minute)} ${y(p.price)}`).join(' ');
  const dots=points.map((p,i)=>`<circle class="contract-observation" data-dollars-per-cent="${dollarsPerCent[i]?.dollars??''}" data-sensitivity-r2="${dollarsPerCent[i]?.r2??''}" cx="${x(p.minute)}" cy="${y(p.price)}" r="0" data-volatility="${p.volatility_60s?.status==='ready'?p.volatility_60s.range_dollars:''}" data-reversals="${p.volatility_60s?.direction_reversals??''}" data-up-moves="${p.volatility_60s?.up_moves??''}" data-down-moves="${p.volatility_60s?.down_moves??''}" data-volatility-pct="${p.volatility_60s?.status==='ready'?p.volatility_60s.range_pct:''}" data-target-delta="${hasTarget?p.price-target:''}" data-observed-at="${escapeHistory(p.observed_at)}" data-price="${p.price}" data-minute="${p.minute.toFixed(2)}" data-time="${escapeHistory(new Date(p.observed_at).toLocaleTimeString())}"/>`).join('');
  let contractLines='';
  if(merged){
    const ceiling=100;
    const cy=price=>t+(1-price/ceiling)*(h-t-b);
    for(const cents of [0,25,50,75,100])axes+=`<text class="yes-axis-label" data-cents="${cents}" x="${w-r+9}" y="${cy(cents)+5}" fill="var(--green)">${cents}¢</text>`;
    for(const [key,color] of [['yes_cents','var(--green)']]){
      const d=quotes.map((p,i)=>`${i?'L':'M'} ${x(p.minute)} ${cy(p[key])}`).join(' ');
      contractLines+=`<path class="merged-contract-line" d="${d}" fill="none" stroke="${color}" stroke-width="1.3" vector-effect="non-scaling-stroke"/>`;
    }
    contractLines+=quotes.map(p=>`<circle class="merged-quote" cx="${x(p.minute)}" cy="${cy(p.yes_cents)}" r="0" data-minute="${p.minute}" data-yes="${p.yes_cents}" data-no="${p.no_cents}"/>`).join('');
  }
  const changeMarkers=(liveChanges||[]).filter(change=>Number.isFinite(change.minute)&&change.minute>=0&&change.minute<15&&['UP / ABOVE','DOWN / BELOW'].includes(change.direction)).map(change=>{
    const up=change.direction==='UP / ABOVE',color=up?'var(--green)':'var(--red)',px=x(change.minute);
    const label=`Live recommendation: ${change.previous_direction} → ${change.direction} · ${new Date(change.observed_at).toLocaleTimeString()} · minute ${change.minute.toFixed(2)}`;
    return `<g class="live-recommendation-change" tabindex="0" role="img" aria-label="${escapeHistory(label)}"><title>${escapeHistory(label)}</title><line x1="${px}" x2="${px}" y1="${t}" y2="${h-b}" stroke="${color}" stroke-dasharray="3 4" opacity=".65" vector-effect="non-scaling-stroke"/><circle cx="${px}" cy="${h-b-12}" r="10" fill="${color}"/><text x="${px}" y="${h-b-7}" text-anchor="middle" fill="#fff" font-size="16">${up?'↑':'↓'}</text></g>`;
  }).join('');
  const initialDirection=tradeRow?.prediction;
  const hasInitialPrediction=['UP / ABOVE','DOWN / BELOW'].includes(initialDirection);
  const initialUp=initialDirection==='UP / ABOVE',initialColor=initialUp?'var(--green)':'var(--red)';
  const initialLabel=`Initial prediction · ${initialDirection} · round start${Number.isFinite(tradeRow?.p_up)?` · UP ${(tradeRow.p_up*100).toFixed(2)}%`:''}`;
  const initialMarker=hasInitialPrediction?`<g class="initial-prediction-marker" tabindex="0" role="img" aria-label="${escapeHistory(initialLabel)}"><title>${escapeHistory(initialLabel)}</title><line x1="${x(0)}" x2="${x(0)}" y1="${t+82}" y2="${h-b}" stroke="${initialColor}" stroke-dasharray="5 4" vector-effect="non-scaling-stroke"/><rect x="${x(0)-12}" y="${t+62}" width="82" height="22" rx="4" fill="${initialColor}"/><text x="${x(0)+29}" y="${t+78}" text-anchor="middle" font-size="14" font-weight="bold" fill="#101820">P ${initialUp?'↑ UP':'↓ DOWN'}</text></g>`:'';
  const tradeEvents=chartTradeEvents(tradeRow);
  const tradeMarkers=tradeEvents.map(event=>{
    const buy=event.kind==='Buy',color=buy?'var(--cyan)':'var(--yellow)',px=x(event.minute),py=buy?t+20:t+48;
    const label=`${event.test?'TEST · ':''}${event.kind} · ${event.direction||''} · ${new Date(event.stamp).toLocaleTimeString()} · minute ${event.minute.toFixed(2)}${event.price!=null?` · ${event.reason==='Settlement'?'payout':event.estimated?'limit':'execution'} ${event.price}¢`:''}${event.reason?` · ${event.reason}`:''}`;
    return `<g class="trade-chart-marker" data-kind="${event.kind}" tabindex="0" role="img" aria-label="${escapeHistory(label)}"><title>${escapeHistory(label)}</title><line x1="${px}" x2="${px}" y1="${py+10}" y2="${h-b}" stroke="${color}" stroke-dasharray="5 4" opacity=".7" vector-effect="non-scaling-stroke"/><rect x="${px-12}" y="${py-10}" width="24" height="20" rx="4" fill="${color}"/><text x="${px}" y="${py+5}" text-anchor="middle" font-size="15" font-weight="bold" fill="#101820">${buy?'B':'C'}</text></g>`;
  }).join('');
  const last=points[points.length-1];
  return `<div class="contract-chart"><svg data-unit="usd" viewBox="0 0 ${w} ${h}" role="img" tabindex="0" aria-label="Asset price and target over the full 15 minute round" onpointermove="if(this.querySelector('.contract-observation'))inspectContractChart(event,this)" onpointerleave="this.querySelector('.contract-cursor').setAttribute('visibility','hidden')" onkeydown="if(this.querySelector('.contract-observation'))inspectContractChartKey(event,this)"><g fill="var(--muted)" font-size="15">${axes}</g>${targetLine}${contractLines}<path class="underlying-price-line" d="${path}" fill="none" stroke="var(--blue)" stroke-width="1.5" vector-effect="non-scaling-stroke"/>${dots}${changeMarkers}${initialMarker}${tradeMarkers}${last?`<circle class="underlying-price-endpoint" cx="${x(last.minute)}" cy="${y(last.price)}" r="3.5" fill="var(--blue)"/>`:''}<g class="contract-cursor" visibility="hidden"><line y1="${t}" y2="${h-b}" stroke="var(--muted)" vector-effect="non-scaling-stroke"/><circle r="4.5" fill="var(--blue)" stroke="var(--panel)" stroke-width="2"/><text class="contract-cursor-time" y="22" fill="var(--muted)" font-size="18" text-anchor="middle"></text><g class="contract-cursor-price" font-size="18" text-anchor="middle"><text class="cursor-price-line" y="45"></text><text class="cursor-range-line" y="68"></text><text class="cursor-reversals-line" y="91"></text><text class="cursor-sensitivity-line" y="114"></text></g></g></svg>${(liveChanges||[]).length?'<div class="muted">Live recommendation changes: <span style="color:var(--green)">↑ UP</span> · <span style="color:var(--red)">↓ DOWN</span> · Hover over a marker for time</div>':''}${hasInitialPrediction?'<div class="muted">P · Initial prediction at round start</div>':''}${tradeEvents.length?'<div class="muted">Trades: <span style="color:var(--cyan)">B · Buy</span> · <span style="color:var(--yellow)">C · Close</span> · Hover for time and contract price · TEST = virtual trade</div>':''}${volatilityHistoryChart(points)}</div>`;
}

function showContractChartPoint(svg,index){
  const points=svg.querySelectorAll('.contract-observation');
  const point=points[Math.max(0,Math.min(index,points.length-1))];
  svg.dataset.index=String(Array.from(points).indexOf(point));
  const x=point.getAttribute('cx'),y=point.getAttribute('cy'),cursor=svg.querySelector('.contract-cursor');
  cursor.setAttribute('visibility','visible');
  const line=cursor.querySelector('line');line.setAttribute('x1',x);line.setAttribute('x2',x);
  const dot=cursor.querySelector('circle');dot.setAttribute('cx',x);dot.setAttribute('cy',y);
  const labelX=Math.max(100,Math.min(900,Number(x)));
  cursor.querySelectorAll('text').forEach(text=>text.setAttribute('x',labelX));
  const timeLabel=cursor.querySelector('.contract-cursor-time');
  timeLabel.textContent=point.dataset.time;
  timeLabel.setAttribute('fill','#c4a5ff');
  let label=cursor.querySelector('.cursor-price-line');
  cursor.querySelectorAll('.contract-cursor-price text').forEach(text=>text.replaceChildren());
  const add=(text,color='var(--muted)')=>{
    const span=document.createElementNS('http://www.w3.org/2000/svg','tspan');
    span.setAttribute('fill',color);
    span.textContent=text;
    label.appendChild(span);
  };
  add(svg.dataset.unit==='usd'?fmtMoney(point.dataset.price):point.dataset.price+'¢','var(--blue)');
  if(svg.dataset.unit==='usd'&&point.dataset.targetDelta!==''){
    const delta=Number(point.dataset.targetDelta);
    add(' · Δ ');
    add(`${delta>0?'+':delta<0?'−':''}${fmtMoney(Math.abs(delta))}`,delta>0?'var(--green)':delta<0?'var(--red)':'var(--yellow)');
  }
  add(' · ');add(point.dataset.minute+' min','#c4a5ff');
  const quotes=Array.from(svg.querySelectorAll('.merged-quote')).filter(quote=>Number(quote.dataset.minute)<=Number(point.dataset.minute));
  const quote=quotes[quotes.length-1];
  if(quote){add(' · Yes ');add(quote.dataset.yes+'¢','var(--green)');}
  if(svg.dataset.unit==='usd'){
    label=cursor.querySelector('.cursor-range-line');
    add('60s range ');
    if(point.dataset.volatility!==''){
      add(fmtMoney(point.dataset.volatility),'var(--yellow)');
      add(' (');add(Number(point.dataset.volatilityPct).toFixed(3)+'%','var(--yellow)');add(')');
    }else add('unavailable');
  }
  if(point.dataset.reversals){
    label=cursor.querySelector('.cursor-reversals-line');
    add('Reversals: ');add(point.dataset.reversals,'var(--cyan)');
    add(' (↑ ');add(point.dataset.upMoves||'—','var(--green)');
    add(' / ↓ ');add(point.dataset.downMoves||'—','var(--red)');add(')');
  }
  const sensitivity=cursor.querySelector('.cursor-sensitivity-line');
  if(sensitivity){
    label=sensitivity;
    add('60s BTC / 1¢ Yes: ');
    if(point.dataset.dollarsPerCent){
      add('≈ '+fmtMoney(point.dataset.dollarsPerCent),'var(--cyan)');
      add(' · R² '+(Number(point.dataset.sensitivityR2)*100).toFixed(0)+'%');
    }else add('unavailable (insufficient data / weak relationship)');
  }
  // Keep each complete line within the SVG, including at the round endpoints.
  const width=svg.viewBox.baseVal.width;
  cursor.querySelectorAll('text').forEach(text=>{
    const half=text.getComputedTextLength()/2;
    text.setAttribute('x',Math.max(8+half,Math.min(width-8-half,Number(x))));
  });
}
function inspectContractChart(event,svg){
  const position=svg.createSVGPoint();position.x=event.clientX;position.y=event.clientY;
  const x=position.matrixTransform(svg.getScreenCTM().inverse()).x;
  const points=Array.from(svg.querySelectorAll('.contract-observation'));
  let nearest=0;
  points.forEach((point,i)=>{if(Math.abs(Number(point.getAttribute('cx'))-x)<Math.abs(Number(points[nearest].getAttribute('cx'))-x)) nearest=i;});
  showContractChartPoint(svg,nearest);
}
function inspectContractChartKey(event,svg){
  if(!['ArrowLeft','ArrowRight'].includes(event.key)) return;
  event.preventDefault();
  const current=svg.dataset.index===undefined?-1:Number(svg.dataset.index);
  showContractChartPoint(svg,current+(event.key==='ArrowRight'?1:-1));
}

function roundTimingCell(row,isCurrent=false){
  const minute=value=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(2):'—';
  const stamp=value=>value?escapeHistory(new Date(value).toLocaleTimeString()):'No observation';
  const priceEvent=['yes','no'].map(side=>{
    const time=row[`last_${side}_cross_50_at`],value=row[`last_${side}_cross_50_minute`];
    return `<span class="round-prob" style="color:var(--${side==='yes'?'green':'red'})" title="${side==='yes'?'Yes':'No'} last upward crossing of 50¢: ${stamp(time)} (first quote above 50¢ counts as the first observed crossing)">${side==='yes'?'Yes':'No'} ↑ 50¢: ${minute(value)}</span>`;
  }).join('');
  const liveArrow=row.last_live_change_at?(row.last_live_direction==='UP / ABOVE'?' ↑':row.last_live_direction==='DOWN / BELOW'?' ↓':''):'';
  const dollars=row.last_live_change_dollars_at_change;
  const percent=row.last_live_change_pct_at_change;
  const sinceStart=row.last_live_change_at?`<span class="round-prob" title="Since-start change captured at the last live direction switch">Since start: ${typeof dollars==='number'&&Number.isFinite(dollars)?fmtSigned(dollars):'—'} (${typeof percent==='number'&&Number.isFinite(percent)?fmtSigned(percent,4)+'%':'—'})</span>`:'';
  const hasChart=row.contract_price_history?.length||row.underlying_price_history?.length;
  const chop=row.choppiness;
  const scoreReady=chop?.status==='ready'&&Number.isFinite(chop.score);
  const scoreSource=chop?.source==='bitcoin'?'BTC':'Yes';
  const scoreRange=chop?.source==='bitcoin'?fmtMoney(chop.range_dollars):`${minute(chop?.range_cents)}¢`;
  const scoreTitle=scoreReady?`Midpoint crossings: ${scoreSource} crossing ${scoreSource==='BTC'?fmtMoney(chop.midpoint):'50¢'} in 15-second samples. First observed side and exact touches do not count. Range: ${scoreRange}. Samples: ${chop.sample_count}/${chop.expected_samples}.${chop.missing_samples?' Gaps ignored.':''}`:`Midpoint crossings unavailable: insufficient saved ${scoreSource} observations or missing round target.`;
  const roundScore=`<span class="round-prob round-choppiness-score" style="color:var(--${scoreReady?'blue':'muted'})" title="${scoreTitle}">Score (${scoreSource}): ${scoreReady?chop.score:'—'}</span>`;
  const quotes=!isCurrent&&hasChart?`<button type="button" class="round-chart-button">Round chart</button>${roundScore}<template class="round-chart-template"><div class="muted">${escapeHistory(row.asset||'BTC')} · ${escapeHistory(new Date(row.round_start).toLocaleString())} · Target ${fmtMoney(row.target)}</div><div class="muted"><span style="color:var(--blue)">● Bitcoin Δ</span> · <span style="color:var(--green)">● Yes</span></div>${!row.underlying_price_history?.length?'<div class="muted">No saved Bitcoin observations for this round</div>':''}${underlyingPriceChart(row.underlying_price_history,row.target,row.contract_price_history||[],row.live_recommendation_changes,row)}</template>`:'';
  return `<span class="round-prob" title="Last live direction change: ${stamp(row.last_live_change_at)}"><span class="round-status muted" title="${escapeHistory(historyStatus(row.status))}" aria-label="${escapeHistory(historyStatus(row.status))}">${row.status==='predicted'?'🔮':row.status==='settled'?'🏁':escapeHistory(historyStatus(row.status))}</span> Live change${liveArrow}: ${minute(row.last_live_change_minute)}</span>`+sinceStart+priceEvent+quotes;
}

function purchaseLiveCell(row){
  const visiblePurchase=row.purchase||row.completed_purchases?.[row.completed_purchases.length-1];
  const live=visiblePurchase?.live_recommendation;
  if(!live) return '<span class="round-empty">—</span>';
  const hit=row.actual?live.direction===row.actual:null;
  const stamp=live.updated_at?new Date(live.updated_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):null;
  return historyDecision(live.direction,live.p_up,hit)+(stamp?`<span class="round-prob">${escapeHistory(stamp)}</span>`:'');
}
function purchaseConditionsCell(purchase){
  if(!purchase) return '';
  const c=purchase.conditions;
  if(!c) return '<span class="round-prob">No Conditions</span>';
  const sources={final:'Final Prediction',live:'Current Prediction',either:'Final or Current Prediction',both:'Final and Current Prediction'};
  const models=Array.isArray(c.required_models)&&c.required_models.length?c.required_models.join(', '):'None';
  const rule=c.rule==='force_buy'?`<div>Force Buy · BRTI distance: ${c.force_buy?.min_distance_dollars==null?'Off':`&gt; $${escapeHistory(c.force_buy.min_distance_dollars)}`}</div>`:c.rule==='fallback'?`<div>Fallback ${escapeHistory(c.fallback_rule_number||1)} · ${c.fallback?.min_distance_dollars===null?'BRTI distance: Off':`BRTI distance &gt; $${escapeHistory(c.fallback?.min_distance_dollars)}`}</div>`:'';
  const stability=c.entry_stability?.enabled?`<div>BTC on buy side of target ≥ ${escapeHistory(c.entry_stability.seconds)} seconds</div>`:'';
  const strength=c.min_live_strength?`<div>Strength ≥ ${escapeHistory(c.min_live_strength)}</div>`:'';
  const trend=c.contract_trend_changes?`<div>Contract price: upward overall trend over ${escapeHistory(c.contract_trend_changes)} changes</div>`:c.contract_upward_changes?`<div>Contract price: ${escapeHistory(c.contract_upward_changes)} consecutive upward changes</div>`:'';
  return `<details class="order-conditions"><summary>Buy conditions</summary><div class="purchase-detail-fields">${rule}${trend}${strength}${stability}<div>Price &gt; ${escapeHistory(c.min_price_cents)}¢ and &lt; ${escapeHistory(c.max_price_cents)}¢</div><div>Purchase minutes: ${c.purchase_minute_from==null?"Not recorded":`${escapeHistory(c.purchase_minute_from)}–${escapeHistory(c.purchase_minute_to)} after round start`}</div><div>Agree with: ${escapeHistory(sources[c.agreement_basis]||c.agreement_basis)}</div><div>Models: ${escapeHistory(models)}</div><div>Final + fresh Live gate: ${c.require_final_live_agreement===true?"On":"Off"} · Minimum model votes: ${escapeHistory(c.min_model_agreement??0)}/4</div><div>CTs: ${escapeHistory(c.contracts)}</div></div></details>`;
}
function purchaseMinuteCell(row,plain=false){
  const purchase=row.purchase;
  if(!purchase) return '';
  let value=purchase.purchase_minute;
  if(typeof value!=='number'){
    const timestamp=purchase.submitted_at||purchase.attempted_at;
    value=timestamp?(Date.parse(timestamp)-Date.parse(row.round_start))/60000:NaN;
  }
  const label=Number.isFinite(value)&&value>=0&&value<=15?value.toFixed(2):'—';
  return plain?label:`<span class="round-prob" title="Purchase minute after round start">${label}</span>`;
}
function purchaseHistoryCell(row,archived=false){
  const purchase=row.purchase;
  const cycles=row.completed_purchases||[];
  if(!purchase&&cycles.length){
    const previous=cycles[cycles.length-1];
    const removed=row.purchase_removed?`<details class="order-conditions"><summary>Removed purchase</summary><button class="secondary confirm-filled" data-asset="${escapeHistory(row.asset)}" data-round="${escapeHistory(row.round_start)}">Restore removed purchase as filled</button></details>`:'';
    return purchaseHistoryCell({...row,purchase:previous,completed_purchases:cycles.slice(0,-1),purchase_removed:false},true)+removed;
  }

  if(!purchase&&!row.prediction) return '<span class="round-empty">No recorded purchase</span>';
  const remove=purchase&&!archived?`<button class="secondary remove-purchase" aria-label="Remove purchase record" title="Remove purchase record from history and statistics" data-asset="${escapeHistory(row.asset)}" data-round="${escapeHistory(row.round_start)}">×</button>`:'';
  if(purchase&&purchase.status==='filled'){
    return '<span class="round-tag green">Filled</span>'+remove;
  }
  const status=purchase?(purchase.status==='cancelled'?'Cancelled in app':purchase.status==='attempted'?'Attempted · unconfirmed':'Unconfirmed'):row.purchase_removed?'Removed':'';
  return `${status?`<span class="round-tag muted">${status}</span>${remove}<br>`:''}<button class="secondary confirm-filled" data-asset="${escapeHistory(row.asset)}" data-round="${escapeHistory(row.round_start)}">Mark filled</button>`;
}
function purchaseProfitCell(row){
  const closed=row.visible_purchase_pnl;
  const open=row.open_purchase_pnl;
  const value=typeof closed==='number'?closed:open?.net_pnl;
  if(typeof closed!=='number'&&!open) return '';
  const color=value==null?'muted':value>0?'green':value<0?'red':'muted';
  const virtual=row.purchase?.test_mode===true;
  const title=virtual?(typeof closed==='number'?'Virtual net P/L after simulated close or settlement; includes estimated fees':'Virtual unrealized net P/L at the fresh displayed contract quote; includes estimated buy and sell fees'):(typeof closed==='number'?'Net P/L after close or settlement; includes buy/sell fees; recorded execution prices used when available, otherwise estimated from limits':'Unrealized net P/L if sold at the fresh held-side bid; includes buy fee and estimated sell fee');
  const label=virtual?'V ':'';
  return `<span class="round-tag ${color}" style="margin:0;padding:0 6px;font-size:11px;line-height:12px" title="${title}">${label}${value==null?'—':`$${fmtSigned(value,4)}`}</span>`;
}

function purchaseDirectionLabel(direction,minute=null){
  const up=direction==='UP / ABOVE',down=direction==='DOWN / BELOW';
  const color=up?'var(--green)':down?'var(--red)':'var(--muted)';
  const arrow=up?'↑ ':down?'↓ ':'';
  const label=up?'UP':down?'DOWN':'Not recorded';
  return `<span class="round-prob purchase-direction" style="color:${color};white-space:nowrap;font-weight:600">${arrow}Buy: ${label}${minute!==null?` <span title="Purchase minute after round start" style="color:var(--text);font-weight:400">· ${minute}</span>`:''}</span>`;
}

function manualCloseButton(row,archived){
  const p=row.purchase,c=p?.close_order;
  if(archived||p?.status!=='filled'||!['attempted','submitted'].includes(c?.status))return '';
  return `<button type="button" class="secondary confirm-closed" data-asset="${escapeHistory(row.asset)}" data-round="${escapeHistory(row.round_start)}" data-token="${escapeHistory(p.attempted_at||p.submitted_at||'')}" data-close-token="${escapeHistory(c.attempted_at||c.submitted_at||'')}">Confirm closed</button>`;
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('.confirm-closed,#confirmClosedStatus');
  if(!button)return;
  event.preventDefault();
  const payload={...(button.closePayload||{asset:button.dataset.asset,round_start:button.dataset.round,purchase_token:button.dataset.token,close_token:button.dataset.closeToken})};
  const value=prompt('Actual closing price per contract in cents (0–100):');
  if(value===null)return;
  const price=Number(value);
  if(!value.trim()||!Number.isFinite(price)||price<0||price>100){
    document.getElementById('robinhoodStatus').textContent='Closing price must be between 0 and 100 cents.';
    return;
  }
  payload.price_cents=price;
  button.disabled=true;
  try{
    const response=await fetch('/api/purchase-closed',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'Could not confirm close');
    await loadRoundHistory();
    await poll();
  }catch(error){document.getElementById('robinhoodStatus').textContent=error.message;}
  finally{button.disabled=false;}
});

function closingBtcDifference(row,purchase){
  if((row.asset||'BTC')!=='BTC') return null;
  const closing=purchase.close_order;
  const target=purchase.target??row.target;
  if(!closing) return row.status==='settled'&&Number.isFinite(row.settlement)&&Number.isFinite(target)?row.settlement-target:null;
  if(Number.isFinite(closing.btc_target_diff_dollars)) return closing.btc_target_diff_dollars;
  if(!Number.isFinite(target)) return null;
  if(Number.isFinite(closing.btc_price)) return closing.btc_price-target;
  const stamp=Date.parse(closing.attempted_at||closing.submitted_at||closing.filled_at);
  let latest=null,latestTime=-Infinity;
  for(const point of row.underlying_price_history||[]){
    const time=Date.parse(point.observed_at);
    if(Number.isFinite(point.price)&&time<=stamp&&stamp-time<=5000&&time>latestTime){
      latest=point.price;latestTime=time;
    }
  }
  return latest===null?null:latest-target;
}

function purchaseExecutionDetails(row,archived=false){
  const purchase=row.purchase;
  if(!purchase) return '';
  const edit=archived?'':` <button class="secondary edit-purchase-price" aria-label="Edit CT price" title="Edit recorded CT price in cents" data-asset="${escapeHistory(row.asset)}" data-round="${escapeHistory(row.round_start)}" data-price="${escapeHistory(purchase.execution_price_cents??purchase.limit_price_cents??'')}">✎</button>`;
  const closing=purchase.close_order;
  const closeStatus=closing?.status==='filled'||closing?.status==='submitted'?'Closed':'Close unconfirmed';
  const closeReasons={stop_loss:'Stop loss',close_conditions:'Early Profit',force_close:'Force Close',force_stop:'Force Close'};
  const closeReason=closeReasons[closing?.reason]?` : ${closeReasons[closing.reason]}`:'';
  const closeInfo=closing?`<span>${closeStatus}${closeReason} · ${tradeMinute(closing.filled_at||closing.submitted_at||closing.attempted_at,row.round_start)}</span><span>limit: ${escapeHistory(closing.limit_price_cents??'—')}¢</span>${closing.execution_price_cents!=null?`<span>Close price: ${escapeHistory(closing.execution_price_cents)}¢</span>`:''}`:row.status==='settled'?`<span>Closed : Settlement · ${tradeMinute(row.round_end,row.round_start)}</span>`:'';
  const closeDelta=closingBtcDifference(row,purchase);
  const closeBtcDiff=closing||row.status==='settled'?`<span title="BTC price minus target at close submission, or settlement price minus target">Close BTC Δ: ${Number.isFinite(closeDelta)?`${closeDelta>=0?'+':'−'}${fmtMoney(Math.abs(closeDelta))}`:'—'}</span>`:'';
  const canForce=!archived&&row.is_current&&purchase.status==='filled'&&!closing;
  const closeOverrides=canForce?['ignore_early_profit','ignore_stop_loss'].map((field,index)=>`<label class="trade-close-toggle" onclick="event.stopPropagation()"><input type="checkbox" class="trade-close-override" data-field="${field}" data-asset="${escapeHistory(row.asset)}" data-round="${escapeHistory(row.round_start)}" data-token="${escapeHistory(purchase.attempted_at||purchase.submitted_at||'')}" ${purchase[field]?'checked':''}>Ignore ${index===0?'Early Profit':'Stop Loss'}</label>`).join(''):'';
  const forceButton=canForce?`<button type="button" class="secondary force-close" data-asset="${escapeHistory(row.asset)}" data-round="${escapeHistory(row.round_start)}" data-token="${escapeHistory(purchase.attempted_at||purchase.submitted_at||'')}" ${purchase.force_close_token||!row.force_close_enabled?'disabled':''} title="Enable Force Close and press to request a close under its independent conditions">${purchase.force_close_token?'Force Close pending':'Force Close'}</button>`:'';
  const price=purchase.execution_price_cents??purchase.limit_price_cents;
  const delta=purchase.btc_target_diff_dollars;
  const btcDiff=`<span title="BTC price minus target at buy submission">Open BTC Δ: ${Number.isFinite(delta)?`${delta>=0?'+':'−'}${fmtMoney(Math.abs(delta))}`:'—'}</span>`;
  return `<div class="purchase-detail-fields closed-trade-execution"><span>${purchase.test_mode?"V · ":""}CTs: ${escapeHistory(purchase.contracts??'—')}</span>${purchaseDirectionLabel(purchase.direction,purchaseMinuteCell(row,true))}<span>price: ${escapeHistory(price??'—')}¢${edit}</span>${btcDiff}${closeInfo}${closeBtcDiff}${manualCloseButton(row,archived)}${closeOverrides}${forceButton}</div>`;
}
function tradeMinute(value,start){
  const minute=(Date.parse(value)-Date.parse(start))/60000;
  return Number.isFinite(minute)&&minute>=0?minute.toFixed(2):'—';
}
function tradeTime(value){
  return value&&Number.isFinite(Date.parse(value))?escapeHistory(new Date(value).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})):'—';
}
function closedTradeTimes(purchase,row={}){
  return `<span>${purchase.test_mode?"TEST · virtual · ":""}· ${tradeTime(purchase.submitted_at||purchase.filled_at||purchase.attempted_at)}</span>${purchaseMinuteCell({...row,purchase})}`;
}

function purchaseDetailsRows(row,key,signature){
  const cycles=row.completed_purchases||[];
  const trades=cycles.map((purchase,index)=>({purchase,number:index+1,archived:true,pnl:row.completed_purchase_pnls?.[index]}));
  if(row.purchase)trades.push({purchase:row.purchase,number:cycles.length+1,archived:false});
  if(!trades.length)return [];
  const html=trades.reverse().map(trade=>{
    const executionRow={...row,purchase:trade.purchase};
    // The most recent archived trade uses the row's selected-trade P/L.
    if(trade.archived&&(row.purchase||trade.number!==cycles.length)){
      executionRow.visible_purchase_pnl=trade.pnl;
      executionRow.open_purchase_pnl=null;
    }
    const conditions=purchaseConditionsCell(trade.purchase).replace(/^<details[^>]*><summary>.*?<\/summary>/,'').replace(/<\/details>$/,'');
    return `<details class="closed-trade-fields"><summary class="trade-line"><span class="closed-trade-heading">T${trade.number}</span>${purchaseProfitCell(executionRow)}<span class="trade-conditions-toggle">Buy conditions</span>${purchaseExecutionDetails(executionRow,trade.archived)}</summary><div class="trade-conditions-content">${conditions}</div></details>`;
  }).join('');
  return [{key:`${key}:trades`,signature,html:`<td colspan="10"><div class="round-purchase-details">${html}</div></td>`}];
}

document.getElementById('orderLogsDialog').addEventListener('click',event=>{
  const dialog=event.currentTarget;
  if(event.target!==dialog)return;
  const bounds=dialog.getBoundingClientRect();
  if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)dialog.close();
});

document.getElementById("roundChartDialog").addEventListener('click',event=>{
  const dialog=event.currentTarget;
  if(event.target===dialog){
    const box=dialog.getBoundingClientRect();
    if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom) dialog.close();
  }
});
document.getElementById('roundHistoryBody').addEventListener('change',async event=>{
  const input=event.target.closest('.trade-close-override');
  if(!input)return;
  const value=input.checked;
  input.disabled=true;
  try{
    const response=await fetch('/api/trade-close-overrides',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({asset:input.dataset.asset,round_start:input.dataset.round,purchase_token:input.dataset.token,field:input.dataset.field,value})});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'Could not update trade');
    await loadRoundHistory();
  }catch(error){input.checked=!value;document.getElementById('roundHistoryCount').textContent=error.message;}
  finally{input.disabled=false;}
});

document.getElementById("roundHistoryBody").addEventListener('click',async event=>{
  const chartButton=event.target.closest('.round-chart-button');
  if(chartButton){
    const template=chartButton.parentElement.querySelector('.round-chart-template');
    document.getElementById('roundChartDialogContent').replaceChildren(template.content.cloneNode(true));
    document.getElementById('roundChartDialog').showModal();
    return;
  }
  const forceButton=event.target.closest('.force-close');
  if(forceButton){
    event.preventDefault();
    forceButton.disabled=true;
    try{
      const response=await fetch('/api/force-close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({asset:forceButton.dataset.asset,round_start:forceButton.dataset.round,purchase_token:forceButton.dataset.token})});
      const data=await response.json();
      if(!response.ok)throw new Error(data.error||'Force Close request failed');
      await loadRoundHistory();
    }catch(error){forceButton.disabled=false;document.getElementById('roundHistoryCount').textContent=error.message;}
    return;
  }
  const button=event.target.closest('.confirm-filled,.remove-purchase,.edit-purchase-price');
  if(!button) return;
  const payload={asset:button.dataset.asset,round_start:button.dataset.round};
  let endpoint=button.classList.contains('remove-purchase')?'/api/purchase-remove':'/api/purchase-filled';
  if(button.classList.contains('edit-purchase-price')){
    const value=prompt('Recorded CT price in cents (above 0, up to 100):',button.dataset.price);
    if(value===null) return;
    const price=Number(value);
    if(!value.trim()||!Number.isFinite(price)||price<=0||price>100){document.getElementById('roundHistoryCount').textContent='CT price must be above 0 and at most 100 cents.';return;}
    payload.price_cents=price;endpoint='/api/purchase-price';
  }
  button.disabled=true;
  try{
    const response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await response.json();
    if(!response.ok) throw new Error(data.error||'Could not update purchase record');
    await loadRoundHistory();
    await poll();
  }catch(error){document.getElementById('roundHistoryCount').textContent=error.message;button.disabled=false;}
});
function updateRoundHistoryRows(rows){
  const body=document.getElementById('roundHistoryBody');
  const existing=new Map(Array.from(body.children).filter(node=>node.dataset.roundKey).map(node=>[node.dataset.roundKey,node]));
  const wanted=new Set(rows.map(row=>row.key));
  Array.from(body.children).forEach(node=>{if(!wanted.has(node.dataset.roundKey)) node.remove();});
  rows.forEach((row,index)=>{
    let node=existing.get(row.key);
    if(!node){node=document.createElement('tr');node.dataset.roundKey=row.key;}
    node.className=row.className||'';
    if(node.rowSignature!==row.signature){
      const openDetails=Array.from(node.querySelectorAll('details')).map(detail=>detail.open);
      node.innerHTML=row.html;
      node.rowSignature=row.signature;
      node.querySelectorAll('details').forEach((detail,i)=>{if(openDetails[i]) detail.open=true;});
    }
    if(body.children[index]!==node) body.insertBefore(node,body.children[index]||null);
  });
  if(!rows.length) body.innerHTML='<tr><td colspan="10" class="muted">No recorded rounds yet.</td></tr>';
}

async function loadRoundHistory(){
  if(roundHistoryBusy) return;
  roundHistoryBusy=true;
  try{
    const filters=new URLSearchParams({page:roundHistoryPage,purchases:document.getElementById("roundPurchaseFilter").value,date_from:document.getElementById('roundDateFrom').value,date_to:document.getElementById('roundDateTo').value});
    document.querySelectorAll('.history-column-filter').forEach(select=>filters.set(select.dataset.column,select.value));
    const response=await fetch(`/api/round-history?${filters}`,{cache:"no-store"});
    if(!response.ok) throw new Error("Could not load round history");
    const data=await response.json();
    renderTrackedAccuracy(data.stats);
    if(roundHistoryAsset!==data.asset){roundHistoryPage=1;roundHistoryAsset=data.asset;}
    else roundHistoryPage=data.page;
    document.getElementById("roundHistoryCount").textContent=`${data.asset} · ${data.total} rounds`;
    document.getElementById("roundHistoryPage").textContent=`Page ${data.page} / ${data.pages}`;
    document.getElementById("roundHistoryPrev").disabled=data.page<=1;
    document.getElementById("roundHistoryNext").disabled=data.page>=data.pages;
    const savedRows=new Map(Array.from(document.getElementById('roundHistoryBody').children).map(node=>[node.dataset.roundKey,node]));
    const renderedRows=data.rounds.flatMap((row,roundIndex)=>{
      const serverNow=Date.parse(data.server_time);
      const isCurrent=Date.parse(row.round_start)<=serverNow&&serverNow<Date.parse(row.round_end);
      row.is_current=isCurrent;
      row.asset=data.asset;
      const key=`${data.asset}:${row.round_start}`,signature=JSON.stringify(row)+String(isCurrent);
      const decorate=items=>{
        const framed=items.map((item,index)=>({...item,className:['round-frame',index===0?'round-group-start':'',index===items.length-1?'round-group-end':''].filter(Boolean).join(' ')}));
        if(roundIndex>0)framed.unshift({key:`${key}:gap`,signature:'gap',className:'round-gap',html:'<td colspan="10" aria-hidden="true"></td>'});
        return framed;
      };
      const reusableKeys=[key,`${key}:timing`];
      if(row.purchase||row.completed_purchases?.length)reusableKeys.push(`${key}:trades`);
      if(reusableKeys.every(rowKey=>savedRows.get(rowKey)?.rowSignature===signature)){
        return decorate(reusableKeys.map(rowKey=>({key:rowKey,signature,html:null})));
      }
      const timingHtml=`<td colspan="10"><div class="round-purchase-details round-timing-fields">${roundTimingCell(row,isCurrent)}</div></td>`;
      const detailsRows=[{key:`${key}:timing`,signature,html:timingHtml},...purchaseDetailsRows(row,key,signature)];
      const models=row.component_results||{};
      const cells=[['Similar-pattern','pattern'],['Recent 3h regime','regime'],['Historical bootstrap','bootstrap'],['Trend/regime','trend']].map(([name,key])=>{
        const m=models[name]||{};
        const storedWeight=row.adaptive_weights?.[key];
        const weight=storedWeight?.weight??storedWeight;
        const weightText=weight!=null&&Number.isFinite(Number(weight))?(Number(weight)*100).toFixed(2)+'%':'Not recorded';
        return `<td>${historyDecision(m.predicted,m.p_up,m.hit)}<span class="round-prob" title="Adaptive weight frozen for this round">⚖ ${escapeHistory(weightText)}</span></td>`;
      }).join('');
      const start=new Date(row.round_start),end=new Date(row.round_end);
      const timeOptions={hour:'2-digit',minute:'2-digit',hour12:false};
      const time=`${start.toLocaleTimeString([],timeOptions)} → ${end.toLocaleTimeString([],timeOptions)}`;
      const pnl=row.purchase_pnl==null?'':`<span class="round-prob ${clsSigned(row.purchase_pnl)}" title="Estimated total for all trades in this round; execution prices when recorded, otherwise limits; default fee 1.94 cents per CT per buy/sale when actual fees are missing">P/L ≈ $${row.purchase_pnl>=0?'+':''}${row.purchase_pnl.toFixed(4)}</span>`;

      const html=`<td>${isCurrent?'<span class="round-current-label">● Current</span>':''}<div class="round-date">${escapeHistory(start.toLocaleDateString())}</div><div class="round-time">${escapeHistory(time)}</div>${pnl}${row.error?'<details><summary>Error details</summary>'+escapeHistory(row.error)+'</details>':''}</td><td><strong>${fmtMoney(row.target)}</strong></td><td>${historyDecision(row.prediction,row.p_up,row.hit)}</td>${cells}<td>${purchaseLiveCell(row)}</td><td>${settlementHistoryCell(row)}</td><td>${purchaseHistoryCell(row)}</td>`;
      return decorate([{key,signature,html},...detailsRows]);
    });
    updateRoundHistoryRows(renderedRows);
  }catch(error){document.getElementById("roundHistoryCount").textContent=error.message;}
  finally{roundHistoryBusy=false;}
}
function changeRoundHistoryPage(delta){roundHistoryPage=Math.max(1,roundHistoryPage+delta);loadRoundHistory();}
const todayParts=new Intl.DateTimeFormat('en-US',{timeZone:'America/Chicago',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
const todayPart=type=>todayParts.find(part=>part.type===type).value;
const todayDate=`${todayPart('year')}-${todayPart('month')}-${todayPart('day')}`;
document.getElementById('roundDateFrom').value=todayDate;
document.getElementById('roundDateTo').value=todayDate;
setInterval(loadRoundHistory,5000);
loadRoundHistory();

let statePollBusy=false;
async function poll(){
  if(statePollBusy)return;
  statePollBusy=true;
  try{
    const r=await fetch("/api/state",{cache:"no-store"});
    const s=await r.json();
    render(s);
  }catch(e){
    document.getElementById("analysisStatus").textContent="Connection error: "+e.message;
  }finally{statePollBusy=false;}
}

setInterval(poll,500);
poll();
</script>
</body>
</html>'''


def parse_runtime_options(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Run the prediction dashboard and Robinhood browser.')
    parser.add_argument('--headless', action='store_true', help='Run Chrome without a display (for SSH/server use).')
    parser.add_argument('--host', default='0.0.0.0', help='Dashboard bind address; use 127.0.0.1 with an SSH tunnel.')
    parser.add_argument('--port', type=int, default=8000, help='Dashboard port (default: 8000).')
    options = parser.parse_args(argv)
    if not 1 <= options.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    return options


if __name__ == "__main__":
    options = parse_runtime_options()
    ARGS.browser_headless = options.headless
    print()
    print("Starting BTC TargetPulse...")
    print(f"Open: http://{options.host}:{options.port}")
    print("Chrome mode:", "headless" if options.headless else "visible")
    print()

    start_history_refresh()

    threading.Thread(
        target=live_worker,
        daemon=True,
    ).start()

    threading.Thread(target=robinhood_worker, daemon=True).start()

    app.run(
        host=options.host,
        port=options.port,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
