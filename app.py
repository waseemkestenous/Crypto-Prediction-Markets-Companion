from flask import Flask, jsonify, request, Response
from types import SimpleNamespace
from collections import deque
from datetime import datetime, timedelta
import threading
import time
import math

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
    source="proxy",
    asset=core.DEFAULT_ASSET,
)

SOURCE = core.BenchmarkSource("proxy", ARGS.asset)
STATS = core.load_stats(ARGS.asset)

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
    "latest_settlement": None,
    "history_message": "Loading historical data...",
}

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
    }


def stats_json():
    total = int(STATS.get("total", 0))
    hits = int(STATS.get("hits", 0))
    misses = int(STATS.get("misses", 0))

    return {
        "total": total,
        "hits": hits,
        "misses": misses,
        "accuracy": round(hits / total * 100.0, 2) if total else None,
    }


def settle_previous_round(previous_end):
    with LOCK:
        p = STATE["prediction"]
        target = STATE["target"]
        samples = list(STATE["samples"])

    if not p or target is None:
        return

    window_start = previous_end - timedelta(seconds=60)

    values = [
        price
        for ts, price in samples
        if window_start <= ts < previous_end
    ]

    if not values:
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


def analyze_target(target, round_start, round_end, token):
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
            target_quality="MANUAL Robinhood target",
            args=ARGS,
        )

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

    except Exception as exc:
        with LOCK:
            if token == globals()["ANALYSIS_TOKEN"]:
                STATE["analysis_status"] = "error"
                STATE["analysis_error"] = str(exc)


def live_worker():
    global ANALYSIS_TOKEN

    start, end = current_round_key()

    with LOCK:
        STATE["round_start"] = start
        STATE["round_end"] = end

    while not STOP_EVENT.is_set():
        loop_start = time.time()
        interval = 3.0

        try:
            current_start, current_end = current_round_key()

            with LOCK:
                stored_start = STATE["round_start"]
                stored_end = STATE["round_end"]

            if stored_start is None or current_start != stored_start:
                if stored_end is not None:
                    settle_previous_round(stored_end)

                with LOCK:
                    ANALYSIS_TOKEN += 1
                    STATE["round_start"] = current_start
                    STATE["round_end"] = current_end
                    STATE["target"] = None
                    STATE["prediction"] = None
                    STATE["live_recommendation"] = None
                    STATE["recommendation_minute"] = None
                    STATE["analysis_status"] = "waiting_target"
                    STATE["analysis_error"] = None

                start_history_refresh()

            price = SOURCE.latest()
            now = now_local()

            with LOCK:
                STATE["live_price"] = float(price)
                STATE["live_updated_at"] = now
                STATE["exchange_quotes"] = dict(SOURCE.last_quotes)
                STATE["exchange_errors"] = dict(SOURCE.last_errors)
                STATE["samples"].append((now, float(price)))
                round_end = STATE["round_end"]
                prediction = STATE["prediction"]
                history_index = HISTORY["index"]
                recommendation_minute = STATE["recommendation_minute"]

            minute_key = max(0, int((now - current_start).total_seconds() // 60))
            if (
                prediction is not None
                and history_index is not None
                and minute_key != recommendation_minute
            ):
                recommendation = core.build_live_recommendation(
                    round_info=prediction,
                    current_price=float(price),
                    now=now,
                    index=history_index,
                    context_hours=ARGS.context_hours,
                )
                with LOCK:
                    if STATE["prediction"] is prediction:
                        STATE["live_recommendation"] = recommendation
                        STATE["recommendation_minute"] = minute_key

            seconds_left = (
                round_end - now
            ).total_seconds() if round_end else 999

            interval = 1.0 if seconds_left <= 65 else 3.0

        except Exception as exc:
            with LOCK:
                if not STATE["analysis_error"]:
                    STATE["analysis_error"] = f"Live price error: {exc}"

        elapsed = time.time() - loop_start
        STOP_EVENT.wait(max(0.15, interval - elapsed))


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

        live_delta = None
        live_delta_pct = None

        if live is not None and target:
            live_delta = live - target
            live_delta_pct = live_delta / target * 100.0

        payload = {
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
            "live": {
                "price": money(live),
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
            "stats": stats_json(),
            "latest_settlement": STATE["latest_settlement"],
            "warning": (
                f"This is a free multi-exchange {ARGS.asset}/USD proxy. "
                "The platform's official settlement can differ."
            ),
        }

    return jsonify(payload)


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
        STATE["prediction"] = None
        STATE["live_recommendation"] = None
        STATE["recommendation_minute"] = None
        STATE["analysis_status"] = "analyzing"
        STATE["analysis_error"] = None

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
        SOURCE = core.BenchmarkSource("proxy", asset)
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
            "samples": deque(maxlen=180), "latest_settlement": None,
            "history_message": f"Loading {asset} historical data...",
        })

    threading.Thread(target=refresh_history, kwargs={"force": True}, daemon=True).start()
    return jsonify({"ok": True, "asset": asset, "status": "refreshing"}), 202


@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Prediction Markets Companion for Robinhood</title>
<style>
:root{
  --bg:#080c12;--panel:#101720;--panel2:#0c121a;--line:#223041;
  --text:#edf3fb;--muted:#8ea0b5;--green:#30d27c;--red:#ff5c6c;
  --yellow:#f5c451;--cyan:#43c9ff;--blue:#669cff
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:20px}
.header{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:16px}
.title{font-size:24px;font-weight:800}
.subtitle{font-size:13px;color:var(--muted);margin-top:4px}
.badge{border:1px solid var(--line);border-radius:999px;padding:8px 12px;color:var(--yellow);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:14px;padding:16px}
.span4{grid-column:span 4}.span5{grid-column:span 5}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}
.label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}
.big{font-size:30px;font-weight:800;margin-top:5px}.medium{font-size:20px;font-weight:750;margin-top:5px}
.muted{color:var(--muted)}.green{color:var(--green)}.red{color:var(--red)}.yellow{color:var(--yellow)}.cyan{color:var(--cyan)}
.row{display:flex;gap:18px;align-items:center;flex-wrap:wrap}.spread{display:flex;justify-content:space-between;gap:12px;align-items:center}
input,select{width:220px;background:#080d14;color:white;border:1px solid #34455a;padding:12px;border-radius:10px;font-size:18px;outline:none}
input:focus,select:focus{border-color:var(--cyan)}
button{padding:12px 16px;border:0;border-radius:10px;font-weight:750;cursor:pointer;background:var(--cyan);color:#00141f}
button.secondary{background:#1b2735;color:var(--text);border:1px solid var(--line)}
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
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <div class="title">Crypto Prediction Markets Companion</div>
      <div class="subtitle">For Robinhood · <span id="assetTitle">BTC</span>/USD · 15-minute prediction with live updates</div>
    </div>
    <div class="row">
      <select id="assetSelect" onchange="setAsset(this.value)">
        <option value="BTC">BTC</option><option value="ETH">ETH</option><option value="SOL">SOL</option>
        <option value="XRP">XRP</option><option value="DOGE">DOGE</option>
        <option value="BNB">BNB</option><option value="HYPE">HYPE</option>
      </select>
      <div class="badge" id="roundBadge">Loading…</div>
    </div>
  </div>

  <div class="grid">
    <div class="card span4">
      <div class="label">Live proxy price</div>
      <div class="big" id="livePrice">—</div>
      <div id="liveDelta" class="muted">Waiting for price…</div>
    </div>

    <div class="card span4">
      <div class="label">Robinhood target</div>
      <div class="big cyan" id="targetDisplay">Not entered</div>
      <div id="timer" class="muted">—</div>
    </div>

    <div class="card span4">
      <div class="label">Tracked accuracy</div>
      <div class="big" id="trackedAccuracy">—</div>
      <div id="trackedDetail" class="muted">No completed rounds</div>
    </div>

    <div class="card span12">
      <div class="spread">
        <div>
          <div class="label">Enter target for current round</div>
          <div class="medium" id="roundText">—</div>
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
        <div class="muted">Prediction will stay frozen during the round.</div>
      </div>
    </div>

    <div class="card span5">
      <div class="label">Live minute recommendation</div>
      <div id="liveRecommendation" class="direction">
        <div class="decision muted">WAITING</div>
        <div class="muted">Available after the frozen prediction is ready.</div>
      </div>
    </div>

    <div class="card span12">
      <div class="label">Round setup</div>
      <div style="margin-top:10px" id="roundSetup" class="muted">—</div>
      <div class="sep"></div>
      <div class="label">Exchange inputs</div>
      <div id="quotes" style="margin-top:8px">—</div>
    </div>

    <div class="card span8">
      <div class="spread">
        <div class="label">Models</div>
        <div class="muted" style="font-size:12px">Probability · adaptive weight · BT accuracy</div>
      </div>
      <table class="models">
        <thead><tr><th>Model</th><th>Above</th><th>Weight</th><th>BT Acc</th></tr></thead>
        <tbody id="modelsBody"><tr><td colspan="4" class="muted">Waiting for prediction…</td></tr></tbody>
      </table>
    </div>

    <div class="card span4">
      <div class="label">Walk-forward backtest</div>
      <div class="big" id="btAccuracy">—</div>
      <div id="btDetails" class="muted">—</div>
      <div class="sep"></div>
      <div class="label">Similar 15m moves</div>
      <div id="moves" class="muted" style="margin-top:8px">—</div>
    </div>

    <div class="card span12" id="settlementCard" style="display:none">
      <div class="label">Latest settlement</div>
      <div id="settlementContent" class="medium" style="margin-top:8px"></div>
    </div>

    <div class="card span12">
      <div class="spread">
        <div id="historyStatus" class="muted">History: loading…</div>
        <button class="secondary" onclick="reloadHistory()">Reload History</button>
      </div>
      <div class="warning" id="warningText" style="margin-top:10px"></div>
      <div class="muted" style="font-size:11px;margin-top:6px">Independent companion tool — not affiliated with or endorsed by Robinhood.</div>
    </div>
  </div>
</div>

<script>
let lastRoundStart = null;
const fmtMoney=v=>v==null?"—":"$"+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const clsSigned=v=>Number(v)>=0?"green":"red";
const fmtSigned=(v,d=2)=>(Number(v)>=0?"+":"")+Number(v).toFixed(d);

function secondsText(sec){
  sec=Math.max(0,Number(sec)||0);
  const m=Math.floor(sec/60),s=sec%60;
  return String(m).padStart(2,"0")+":"+String(s).padStart(2,"0");
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

function render(s){
  const asset=s.asset||"BTC";
  document.getElementById("assetTitle").textContent=asset;
  document.getElementById("assetSelect").value=asset;
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
  document.getElementById("livePrice").textContent=fmtMoney(live.price);

  const ld=document.getElementById("liveDelta");
  if(live.delta!=null){
    ld.className=clsSigned(live.delta);
    ld.textContent=`${fmtSigned(live.delta)} (${fmtSigned(live.delta_pct,4)}%) · ${live.position} target`;
  }else{
    ld.className="muted";
    ld.textContent="Waiting for target comparison…";
  }

  document.getElementById("targetDisplay").textContent=
    s.target==null?"Not entered":fmtMoney(s.target);

  const stats=s.stats||{};
  if(stats.accuracy==null){
    document.getElementById("trackedAccuracy").textContent="—";
    document.getElementById("trackedDetail").textContent="No completed rounds";
  }else{
    const el=document.getElementById("trackedAccuracy");
    el.textContent=stats.accuracy.toFixed(2)+"%";
    el.className="big "+(stats.accuracy>=50?"green":"red");
    document.getElementById("trackedDetail").textContent=
      `${stats.hits} HIT / ${stats.misses} MISS`;
  }

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

    const hurdleClass=p.required_move_dollars>=0?"green":"red";
    document.getElementById("roundSetup").innerHTML=
      `Start ref: <b>${fmtMoney(p.start_reference)}</b><br>`+
      `Hurdle: <b class="${hurdleClass}">${fmtSigned(p.required_move_dollars)} (${fmtSigned(p.required_return_pct,4)}%)</b><br>`+
      `Agreement: <b>${p.agreement}/4</b>`;

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
      `${bt.rounds} rounds · ${bt.hits}H/${bt.misses}M<br>`+
      `Brier: ${bt.brier==null?"—":bt.brier.toFixed(4)} · Calibration: ${bt.calibration_samples}`;

    document.getElementById("moves").innerHTML=
      `P10 ${fmtSigned(p.moves.p10,4)}%<br>`+
      `Median ${fmtSigned(p.moves.median,4)}%<br>`+
      `P90 ${fmtSigned(p.moves.p90,4)}%`;
  }else{
    box.className="direction";
    box.innerHTML='<div class="decision muted">WAITING FOR TARGET</div><div class="muted">Prediction will stay frozen during the round.</div>';
    document.getElementById("roundSetup").textContent="—";
    document.getElementById("modelsBody").innerHTML='<tr><td colspan="4" class="muted">Waiting for prediction…</td></tr>';
    document.getElementById("btAccuracy").textContent="—";
    document.getElementById("btDetails").textContent="—";
    document.getElementById("moves").textContent="—";
  }

  const rec=s.live_recommendation;
  const recBox=document.getElementById("liveRecommendation");
  if(rec){
    const up=rec.recommendation.includes("UP");
    const statusClass=rec.changed?"red":"green";
    recBox.className="direction "+(up?"up":"down");
    recBox.innerHTML=`
      <div class="decision ${up?"green":"red"}">${rec.recommendation}</div>
      <div class="medium">${rec.confidence.toFixed(2)}% · ${rec.strength}</div>
      <div class="${statusClass}" style="font-weight:800;margin-top:8px">${rec.status}</div>
      <div class="muted" style="margin-top:8px">
        Minute ${String(rec.minute).padStart(2,"0")} · ${rec.remaining_minutes} min left<br>
        Since start: <span class="${clsSigned(rec.change_dollars)}">${fmtSigned(rec.change_dollars)} (${fmtSigned(rec.change_pct,4)}%)</span>
      </div>`;
  }else{
    recBox.className="direction";
    recBox.innerHTML='<div class="decision muted">WAITING</div><div class="muted">Available after the frozen prediction is ready.</div>';
  }

  const quotes=s.quotes||{};
  const qEntries=Object.entries(quotes);
  document.getElementById("quotes").innerHTML=qEntries.length
    ?qEntries.map(([k,v])=>`<span class="quote">${k} <b>${fmtMoney(v)}</b></span>`).join("")
    :'<span class="muted">Waiting for exchange quotes…</span>';

  const h=s.history||{};
  document.getElementById("historyStatus").textContent=
    "History: "+(h.message||"—");

  document.getElementById("warningText").textContent=s.warning||"";

  const st=s.latest_settlement;
  const sc=document.getElementById("settlementCard");
  if(st){
    sc.style.display="block";
    document.getElementById("settlementContent").innerHTML=
      `${st.hit?'<span class="green">HIT ✓</span>':'<span class="red">MISS ✗</span>'} · `+
      `${st.prediction} ${st.confidence.toFixed(2)}% · `+
      `Target ${fmtMoney(st.target)} → Settlement ${fmtMoney(st.settlement)} · `+
      `${st.sample_count} samples`;
  }
}

async function poll(){
  try{
    const r=await fetch("/api/state",{cache:"no-store"});
    const s=await r.json();
    render(s);
  }catch(e){
    document.getElementById("analysisStatus").textContent="Connection error: "+e.message;
  }
}

setInterval(poll,1000);
poll();
</script>
</body>
</html>'''


if __name__ == "__main__":
    print()
    print("Starting Crypto Prediction Markets Companion for Robinhood...")
    print("Open: http://0.0.0.0:5000")
    print()

    start_history_refresh()

    threading.Thread(
        target=live_worker,
        daemon=True,
    ).start()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
