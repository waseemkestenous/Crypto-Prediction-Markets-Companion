BTC TargetPulse

python -m pip install -r requirements.txt
python app.py

The dashboard:
- supports BTC, ETH, SOL, XRP, DOGE, BNB, and HYPE with an in-page asset selector
- keeps separate accuracy statistics for every asset
- lets you enter the Robinhood target in the browser
- freezes the original prediction for the current 15-minute round
- updates a separate live recommendation every 5 seconds and says whether
  it is still the same or has changed based on the live price, the move from
  round start, and the remaining time
- parses the CF Benchmarks BRTI page for live BTC/USD prices
- uses the multi-exchange proxy for other assets
- shows UP/DOWN probabilities, model weights, backtest, price source inputs
- records sampled HIT/MISS at the next round boundary

IMPORTANT:
BTC live price is parsed from https://www.cfbenchmarks.com/data/indices/BRTI
using the embedded indexSummary.value. Fetch/parse errors are reported without
switching BTC to exchange prices. Other assets and historical candles use
a multi-exchange USD proxy. Website snapshots and sampled settlement can lag.
The platform's official settlement can differ.
Independent companion tool; not affiliated with or endorsed by Robinhood.

TERMINAL EXAMPLES
python brti_engine.py --asset BTC
python brti_engine.py --asset ETH
python brti_engine.py --asset SOL
python brti_engine.py --asset XRP
python brti_engine.py --asset DOGE
python brti_engine.py --asset BNB
python brti_engine.py --asset HYPE

AUTOMATIC ROBINHOOD TARGET
Starting app.py opens a visible Chrome window with a separate persistent profile.
Chrome must be installed. For first login (especially Sign in with Google):
1. Stop app.py and close its Robinhood Chrome window.
2. Run: python robinhood_target.py --login
3. Sign in manually in ordinary Chrome, then close that Chrome window.
4. Run: python app.py
The login command uses the same dedicated profile without Playwright or automation.
Do not run login mode and app.py at the same time.
The script opens the configured event URL, follows “View current event” when expired,
and verifies the selected asset and exact round dates before saving the target.
It automatically freezes the prediction once per round and checks again next round.
The current target and source URL are saved immediately in current_target.json;
completed predictions continue to be recorded in data.json as before.
Browser/login/parse errors appear on the dashboard. Manual target entry still works.
The supplied URL is for BTC; other assets require a matching Robinhood event URL
in robinhood_target.py and will not be populated from the BTC event.

At each new 15-minute round boundary the browser opens the current event and
reads its target. It stays on the existing round throughout the final minute;
there is no next-round prefetch. Future-round targets are rejected.

ROBINHOOD ORDER DRAFT
For the frozen prediction of the current round, the same event page selects
Buy Yes for UP / ABOVE or Buy No for DOWN / BELOW, and Buy In: Contracts.
Only if the selected side's displayed Limit Price is strictly below 60 cents,
the script fills Contracts: 1 and fixes that displayed price as the limit.
At 60 cents or higher it waits and checks again. It prepares at most one draft
per round, verifies the event/target against the prediction, and never clicks
Review order or submits an order. Review and submission are manual in Robinhood.

If the event shows “Target price: TBD”, the browser waits 5 seconds and reloads
until a numeric target is published. Missing/mismatched current-round target data
also triggers a refresh retry. No form is prepared while TBD is displayed, even
if an older numeric target remains elsewhere in the page.

Expired-event navigation has priority over refresh: the browser follows a visible
“View current event” button, link, or text control and waits for the event URL to
change. Before retrying a refresh it checks the banner again, including after
a TBD wait, so it does not repeatedly refresh the expired starting URL.

Target parsing uses the visible event heading rather than a potentially stale
browser title, normalizes wrapped text, and accepts a consistent numeric target
from the contract's “or above” label or About. Exact round dates are required
from About or the heading plus Timeline Event day. Rejection reasons (including
observed vs expected round timestamps) are now shown on the dashboard.

Event loading: the reader waits up to 30 seconds for the visible 15-minute
market heading, even if the site shell contains other h1 elements or the tab
title still says Home. Unicode formatting characters and spacing are normalized
before asset validation. A rejected asset reports the actual heading.

ROUND HISTORY
The bottom of the dashboard lists recorded rounds for the selected asset, newest
first (25 per page). It shows round date/time, target, combined frozen prediction,
each of Pattern/Recent/Bootstrap/Trend with P(Above) and HIT/MISS when settled,
and sampled settlement/result. Pending, failed, and unsampled rounds remain
visible with no fabricated HIT/MISS.
Round start, target, frozen model predictions, and settlement are journaled in
round_history_<asset>.json. Existing completed rounds from data.json/data_<asset>.json
are included, and result files no longer discard entries beyond 1000 rounds.
Only rounds observed while the application runs or previously saved can be shown;
previously deleted rounds and predictions from periods offline cannot be recovered.

MODEL AGREEMENT GATE
Order-form automation requires Final prediction, Pattern, Recent, Bootstrap,
and Trend to agree on the same frozen UP/ABOVE or DOWN/BELOW direction for the
current round. A missing/nonfinite model output or any disagreement skips form
preparation. The existing price-below-60-cents and one-contract conditions remain.

EDITABLE CONTRACT SETTINGS
Contract settings on the dashboard let you save a strict maximum price threshold
in cents (greater than 0, up to 100) and a positive whole contract quantity.
Defaults remain below 60 cents / 1 contract. Settings are persisted in
order_settings.json and included in the frozen prediction snapshot used by the
form worker. Changing them during preparation cancels that preparation on the
next validation. Existing drafts/submitted orders are not changed by this setting.

TRACKED PURCHASE ACCURACY
Tracked accuracy now uses only rounds with confirmed filled purchases and an
observed settlement direction: correct purchased directions / settled purchased
rounds. Filled orders awaiting settlement, submitted/unconfirmed orders, and
rounds with no recorded purchase are excluded. Multiple fills of the same round
are counted as one round. All-prediction model stats remain available internally.
The browser records successful Buy-click returns as submitted, not filled, and
only promotes them automatically when a visible Done button and Filled quantity
N of N confirm the full saved contract quantity for the matching event. Other receipt formats,
late fills, or old purchases can be confirmed through Mark filled in round history
after the user verifies the order in Robinhood. Past fills cannot be inferred
from old HIT/MISS records. Accuracy still uses the companion's sampled settlement.
At round end, attempted/submitted purchases without fill confirmation become
Cancelled in app. This preserves their details and excludes them from filled
purchase statistics; it does not cancel an order in Robinhood. A later verified
fill can still be recorded with Mark filled.

LIVE RECOMMENDATION EXCEPTION
The four frozen component models must agree with each other. If their shared
direction agrees with the final prediction, the existing gate allows it. If
their shared direction instead agrees with a fresh Live minute recommendation
and differs from the final prediction, the form uses the live direction.
Live recommendations older than two refresh intervals cannot enable this
exception. Price/quantity settings remain in effect. The recorded purchase
direction follows the selected side, so purchased-round accuracy uses the
actual selected direction even when it differs from the original final prediction.

LIVE SNAPSHOT IN ROUND HISTORY
Live at purchase displays the direction, its probability, and update time of
the live recommendation captured when the order was submitted. The snapshot
is kept under purchase.live_recommendation and does not change with later live
updates or fill confirmation. The Purchase column separately shows execution
status. Older orders without a captured snapshot display Not recorded.

SELECTABLE BUY CONDITIONS
Open Buy conditions under Contract settings, optionally select model agreement filters
(Pattern, Recent, Bootstrap, Trend), and choose Final prediction, fresh Live,
either, or Final and fresh Live as the agreement source. The last option
requires Final and fresh Live to have the same direction. All selected models must agree with that
source; unselected models do not block the order. Leave all models unchecked
to use the selected prediction source without component agreement filters.
Either prefers Final when
both qualify. Save persists the choice and applies it to the next preparation.
Older settings default to all four models and either source. The existing
price threshold, quantity, target checks and fresh Live snapshot requirement
remain in effect for every order.

MINIMUM BUY PRICE
Contract settings also includes Price > (cents). Orders require a price strictly
above this minimum and strictly below Price <. The minimum defaults to 0 for
older saved settings and must be nonnegative and below the maximum. Save applies
the range to the next preparation; price checks enforce both bounds throughout.

PURCHASE CONDITION HISTORY
Save settings persists the chosen buy conditions. Each new purchase additionally
records its price bounds, agreement source, selected models, and quantity before
submission. Purchase conditions in round history displays that saved snapshot,
which survives fill confirmation and later settings changes. Older purchases
without a snapshot show Conditions not recorded.

CURRENT CONTRACT QUOTES
All automatic purchases, including fallback rules, now require the purchased
side's overall price trend over the configured number of distinct changes to
be upward. Set Changes under Trade Conditions and Save; the default is 10
changes (11 quotes). N changes require N+1 quotes. Each FB rule has its own Changes setting, initially inherited from the shared
value for existing rules, and the effective value is saved with each purchase. A least-squares line is fitted to price against observation time;
its slope must be positive. Individual downward changes are allowed.
Yes is checked for Up; the estimated No quote is checked for Down. Unchanged
quotes do not count. Missing history, a stale current quote, or a flat/downward overall trend
blocks purchase until the condition is met again. Only the current asset, target
and round's observations are used. Before Review and again before Buy, the browser
checks the visible event target and current quote; a nonpositive overall slope
aborts submission. The condition is mandatory and recorded as contract_trend_changes
and contract_trend_method
in purchase conditions. It is an entry filter, not a guarantee of profit.

The separate Contract prices during this round section shows the first observed
quote and each subsequent Yes/No price change, with observation time and elapsed
round minute, plotted as a connected blue line on the dashboard’s dark background. Choose Yes or estimated No
to show one contract at a time. The time axis spans the full round from 0 to 15 minutes; the price axis shows
cents from 0 to 100 (expanded when the estimated No quote exceeds 100).
Horizontal grid lines remain visible, and unobserved times have no plotted prices. Move the pointer along the line to show a
vertical cursor, observation time and cent price; arrow keys also select points. These are contract cents, not the underlying asset
price. Samples are saved in contract_price_history in the asset's round journal
and can be expanded under Contract prices in round history after the round ends.
Repeated unchanged quotes are omitted. No remains an estimate from Yes; these
are observed quotes, not execution prices or a complete exchange tick feed.
Tracking begins when the application observes quotes; offline periods are missing.

The contract chart shows current Yes and estimated No cent quotes and the latest
quote update time inside the chart area. The target card displays the target. Select a contract takes precedence over the header
quote when both are visible. No is estimated using 100 cents - Yes + 1 cent,
including fractional cents; this formula can exceed 100 cents when Yes is below
1 cent. Quotes must match the current asset, target and round and expire after
10 seconds without an update. This display does not change order limit pricing.

PURCHASE MINUTE WINDOW
Buy conditions includes From minute and To minute, measured after the round
starts. Defaults are 0 and 15. Values must satisfy 0 <= From < To <= 15. From
is inclusive and To is exclusive; orders cannot proceed after the round ends.
The window is checked during preparation and again before Buy. Save settings
persists the window, and each purchase condition snapshot includes its bounds.

REMOVE PURCHASE RECORD
The x beside a purchase removes its record from displayed purchase history,
accuracy and balance calculations while keeping the round. This changes only
the app journal. Mark filled restores the archived details. Automatic receipt
updates cannot restore a record explicitly removed by the user.

ROUND EVENT TIMESTAMPS
Each round records the latest observed direction switch between consecutive
live recommendations, and the latest observation of each contract price
strictly above 80 cents. Up uses the parsed Yes quote; Down uses the calculated
No quote displayed in the app. An unchanged direction does not update the
change timestamp. Round history shows these times below the round time.
Missing historical observations show a dash; they cannot be reconstructed.

Round event history displays elapsed decimal minutes from 0.00 to 15.00,
with two decimal places (for example 7.50 means 7 minutes 30 seconds). These
minute numbers are persisted alongside timestamps. Existing recorded event
timestamps are converted to saved minute values when history is loaded.

The last live direction change also freezes its Since start dollar and percent
values. Later recommendations with the same direction do not update these
values; the next switch replaces them. Older changes without snapshots show
a dash for the missing values.

Above-80 history now displays the FIRST observed minute for the most recently
observed qualifying side. First observations are frozen independently for Up
and Down; later quotes do not overwrite them. Last observations remain saved
to choose which side to display. Past last-only records cannot reconstruct the
first observation and show a dash until a first observation is recorded.

FALLBACK PURCHASE RULE
Enable Fallback under Buy conditions and Save. Defaults: minutes 13 <= elapsed
< 15, CF Benchmarks BRTI website price more than $50 above/below target in
the fresh Current Prediction direction, contract limit price strictly between
70 and 90 cents, and 1 CT. All parameters and quantity are editable. The
primary rule is attempted first; fallback runs only if it does not purchase.
Multiple fallback rules can be added, removed and reordered in the buy settings.
Buy conditions includes a saved Time in force selection: Immediate or cancel
(IOC, default) or Good till date. It applies to primary and fallback purchases.
Close conditions has an independent Time in force selection, also defaulting
to IOC. The browser selects and verifies the saved value for each order type.
Each rule has its own enabled flag, minute window, BRTI distance, price bounds
and contract quantity. Save persists the list. Enabled rules run from top to
bottom after the primary rule; checking stops after a submission. The existing
single fallback is loaded as the first rule. Only one active purchase at a time
is allowed. Buy conditions includes Allow multiple buys in the same round after
full close (enabled by default to preserve the current behavior). When disabled,
the completed purchase blocks another automatic buy until the next round.
When enabled, a confirmed full close permits another buy in the same round after
a browser refresh, under the current buy conditions. Completed cycles are
preserved in completed_purchases and shown under Previous closed trades.
Purchase history records the selected fallback number and conditions.

Close conditions are disabled by default. Enable Close all and Save to allow
automatic sales of confirmed holdings: the held Yes/No contract's visible bid
must be strictly between the configured cents bounds, within the configured
minutes after round start (From included, To excluded). Defaults are 85–95 cents
and minutes 5–10. The browser validates the side, target, available quantity and
limit, fills all available contracts and selects IOC. Close all opens the review
screen; the browser verifies side, target, quantity and limit there, then
submits with Close. Close all alone is not recorded as a submitted sale.
Sale attempts and submissions are stored separately under purchase.close_order;
Done and matching full Filled quantity are required to mark the sale filled.
The app does not retry a recorded sale attempt automatically. History displays
Close unconfirmed, Close submitted or Closed. Round P/L sums all archived
completed trades plus the current trade. Filled closes use sale minus buy cost
and fees; settled holdings use the payout minus buy cost and fees. Pending
trades are excluded until priced and completed. Execution prices are preferred;
limits are used as estimates when execution prices are missing. Missing fees
use 1.94 cents per contract per buy/sale. P/L is labeled approximate. Balance
and counted purchases include each completed trade, not just the latest one.
Purchase HIT/MISS also includes archived trades: confirmed closed trades use
positive/negative modeled net P/L, while holdings settled at round end compare
the purchased direction with the settlement. Break-even closes are shown
separately and excluded from the HIT/MISS percentage. Final prediction accuracy
still counts settled round predictions. Date and purchase filters apply to both.
An active recorded purchase or submission attempt blocks another purchase in
the round. Removing it with X permits a new order under the current conditions;
the removed details remain archived and the prior Robinhood order is not cancelled.
Fallback requires BTC website BRTI
quotes no older than 10 seconds, uses the selected-side order form price,
and records its rule, BRTI quote and conditions with the purchase.

LINUX SERVER OVER SSH
Install Python dependencies and Chrome on the server (Ubuntu/Debian example):
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python -m playwright install --with-deps chrome

First login: your client must have an X server (Linux desktop, or an X server
for macOS/Windows), and the server must support SSH X11 forwarding with xauth.
From your client:
  ssh -X user@server
On the server, from the app directory:
  .venv/bin/python robinhood_target.py --login
Sign in manually, then close the Chrome window. Do not run the app concurrently
with login Chrome; both use the same .robinhood-browser profile on the server.
If X11 is unavailable, use a remote desktop session on the server for login.

Run without a desktop afterward:
  .venv/bin/python app.py --headless --host 127.0.0.1 --port 8000
From another local terminal, forward the dashboard:
  ssh -N -L 8000:127.0.0.1:8000 user@server
Open http://127.0.0.1:8000 on your local computer.
Keep the server process running in tmux or a service after SSH disconnects.
For a visible app browser over X11, omit --headless inside the ssh -X session.
Headless mode still requires a valid Robinhood login; if it expires, stop the
app and repeat the visible login. Browser/profile data must remain private and
must not be used concurrently. Log in fresh on the server instead of assuming
an existing desktop profile will migrate successfully.

References:
https://playwright.dev/python/docs/browsers
https://man.openbsd.org/ssh

STOP LOSS
Close conditions includes independent Stop loss (enabled by default), an independent Time in force selector, and Drop
from peak bid, default 20 cents. It closes confirmed holdings when the held
side's actual visible bid is at or below the highest observed bid since fill
minus the saved drop, even
if Close all is disabled or its price/minute conditions do not match. The
peak bid is journaled under purchase.stop_loss_peak_bid_cents and never decreases.
The first observed matching bid initializes the peak; unobserved past highs cannot
be reconstructed. Each new purchase starts a separate peak.
Only matching current-round confirmed holdings qualify. Stop-loss sales use their own saved Time in force (IOC by default or Good till date),
verify the held side, target, available quantity and review, and record reason
stop_loss plus its settings. Done and full Filled quantity are still required to
confirm closure; a submitted sale does not guarantee execution. Existing recorded
close attempts/submissions are observed rather than submitted again. Save applies
the edited threshold; the drop is cents per contract and excludes fees.

MINIMUM LIVE STRENGTH
Trade Conditions includes Strength >=, default MODERATE. Each FB rule has its
own Strength setting; existing rules initially inherit the shared value. A fresh Live minute
recommendation must meet this minimum for both primary and fallback purchases,
regardless of the selected prediction agreement basis. MODERATE and STRONG qualify
at the default; weaker or missing/nonfinite confidence blocks buying. The level
uses the same confidence-strength boundaries as the dashboard. Save persists the
selection; it is rechecked throughout preparation and before Buy and recorded in
each purchase's conditions. This does not change sell or stop-loss conditions.

HISTORICAL BUY CONDITIONS ANALYSIS
An independent dashboard card compares 1,944 combinations of strict price bounds,
minute windows within 0–15, 3/5/10/20 trend changes, and frozen model agreement
with Final prediction. Only completed rounds with matching observed quotes are
replayed. The first eligible quote buys one simulated contract and holds to
settlement, subtracting an estimated 1.94-cent buy fee and no settlement sale fee.
The oldest 70% of usable rounds select the candidate (highest training net P/L,
at least three entries); the newest 30% validate it without changing selection.
Training, validation and combined results show entry counts and modeled P/L.
Fewer than 30 quote rounds are marked exploratory; validation support requires
at least three entries and positive P/L. Smaller/negative holdouts are explicitly
marked unconfirmed. No conditions are changed automatically. Live strength,
fallback BRTI distance and stop/early-close rules cannot be fully replayed from
this journal; No prices are estimated and observed quotes are not executable form
prices. The card refreshes each minute and supports manual Refresh analysis.

OPEN HOLDING LIVE P/L
Live at purchase now includes Winning / Losing / Break-even for confirmed open
current-round holdings, estimated from a matching held-side bid no older than
10 seconds. Buy fees plus estimated sell fees are deducted. Missing/stale bids
show Live P/L unavailable. This unrealized estimate is separate from settled P/L
and balance. The reader observes the Close panel even when sale rules are disabled;
no sale is submitted unless existing close/stop conditions qualify.

UNCONFIRMED ATTEMPTS
Automatic 30-second cancellation/removal and refresh are disabled. Unconfirmed
purchases remain recorded until confirmed or manually removed. Existing round-end
local cancellation of unconfirmed records remains unchanged.

BUY REVIEW SCREEN
After Review order, the reader waits for the final Buy control and verifies the
review's selected Yes/No side, target, quantity, limit and saved Time in force.
The final trend check can read the review's current Ask when the original
[target] or above cent quote is no longer visible. It never treats the order's
limit as a current market quote. Invalid review details are rejected and refreshed;
recorded unconfirmed attempts remain pending until confirmed or manually removed.

STOP-LOSS IOC MENU
The Close form selector is scoped to the matching held-side panel, but its menu
is searched at page level because Robinhood can render it outside that panel.
IOC options with descriptions and menuitemradio roles are supported. Selection
waits for the control's displayed value to update before proceeding; an ambiguous
option or unverified value blocks submission.

REVIEW REJECTION LIFECYCLE
The attempted purchase is now saved only after final window/Live/settings and
quote-trend checks pass, immediately before clicking Buy. A rejected review is
refreshed back to the event form without creating a blocking attempted record.
A Buy click that errors remains an unconfirmed attempt because submission may
have occurred; no automatic cancellation timer is enabled.

ROBINHOOD AVAILABLE BALANCE
Current balance displays the dollar amount labelled available in the Robinhood
browser, rather than initial balance plus modeled P/L. The source and observation
time are displayed. Ambiguous/missing readings are ignored; after 30 seconds
without a valid observation the display becomes unavailable (no calculated
fallback). Modeled trade P/L and the editable starting balance remain separate.

BRTI SOURCE TIMESTAMP AND STALENESS
BTC still reads https://www.cfbenchmarks.com/data/indices/BRTI, using the embedded
indexSummary.value and indexSummary.lastUpdated (Unix milliseconds). Fetch time
is separate from source time. Missing, future or older-than-20-second source
quotes are rejected instead of being marked fresh on receipt. The dashboard
shows source time/age and a stale warning; stale reads invalidate Live and block
price-dependent buying, including fallback BRTI gaps. Website polling is normally
3 seconds and 1 second during the final 65 seconds; this is not a live data feed
and cannot remove publication/network latency. No exchange-price substitution.

LIVE RENDERED BRTI WEBSITE
The preferred BTC reader now keeps one shared read-only headless Chrome page
open at the same CF Benchmarks BRTI URL, allowing the site's JavaScript to update
the visible summary. It reads the displayed BRTI price and Last updated timestamp
every second. No Robinhood profile, login or API credentials are used. If that
browser is unavailable, the original HTML reader is attempted, subject to the
same 20-second source-age rejection. The UI identifies Live rendered website vs
HTML snapshot. Older HTML timestamps cannot overwrite a newer browser reading.
This reduces the measured HTML snapshot delay but still depends on website
publication latency and availability; stale quotes remain blocked.

MONOTONIC BRTI UPDATES
All browser and HTML quotes now pass one locked acceptance check. Older or equal
source timestamps cannot replace the accepted price/time pair or renew its age.
A last accepted quote may be reused only while its real source age is <=20 seconds.
Rejected stale readings never overwrite the displayed last-observed pair. The
application publishes one atomic snapshot and also refuses backward display times.

UNDERLYING PRICE ROUND CHART
A separate Bitcoin/asset price chart shows fresh observed source quotes during
the active round, against a fixed 0–15 minute time axis. The yellow dashed line
marks the current Robinhood target; the blue line shows USD price. Pointer/arrow
inspection displays price, source time and minute. Gaps over 15 seconds are not
connected. Up to 2,000 distinct timestamped observations are retained in memory
for the full round, reset on a new round or asset switch. Collection starts while
the app runs; missing earlier/offline points are not fabricated. The section has
its own collapse arrow.

MERGED CHART TRIAL
The active-round chart defaults to Merged charts (trial): Bitcoin/asset USD price
and target use the left axis; Yes and estimated No cents use the right axis, on
the same 0–15 minute timeline. Colors distinguish all three price lines and the
target. Current contract quotes remain above the plot. Choose Separate charts
in the chart header to restore the previous two sections immediately; the layout
preference is saved in this browser and does not affect trading conditions.

ALIGNED TARGET / YES AND PERSISTENT UNDERLYING HISTORY
The merged chart now plots only Yes on a fixed 0–100 cent right axis. The USD
axis is symmetric around the target so its horizontal line aligns exactly with
50 cents. Price observations are appended per asset/round to price_history/*.jsonl
and restored when that round is initialized after restart. Existing observations
are retained; no offline or previously unrecorded prices are fabricated. The
per-round files are excluded from git.

BRTI age policy: source quotes up to and including 20 seconds old are usable;
quotes older than 20 seconds block price-dependent buying. This shared limit
applies to rendered website/HTML validation, displayed freshness and fallback.

BRTI timing controls are independent: rendered source checks and HTML fallback
requests occur at 10-second intervals; maximum permitted source quote age is
20 seconds. Dashboard repainting does not refresh the source timestamp.

STOP-LOSS PRICE CEILING
Stop loss includes Price < (cents), default 100 for old settings. A stop-loss
sale requires both the saved drop from the observed peak and a held-side bid
strictly below this ceiling (equality does not trigger). It is validated before
preparation and again in review, and stored with the stop-loss sale snapshot.
Normal Close all rules remain independent.

MERGED CHART AS THE ONLY VIEW
The layout selector and separate contract-price panel are removed. The main
chart uses Bitcoin/asset delta plus Yes, with target 0 aligned to 50 cents.
Past rounds expand a Round chart using the saved underlying observations and
contract quotes for that exact asset/round. Old rounds without underlying samples
explicitly show missing Bitcoin history; no prices are reconstructed. The current
round still uses only the main chart rather than a duplicate in the history row.

SIGNED FALLBACK BRTI DISTANCE
BRTI delta thresholds accept negative dollar values. The directional gap is
BRTI minus target for Live UP, and target minus BRTI for Live DOWN. Buying
requires that gap to be strictly greater than the saved threshold. For example,
-30 permits UP below target by less than $30, or DOWN above target by less
than $30. Equality is blocked; zero still requires the Live side of target.
All other purchase checks remain in force.

STOP-LOSS BRTI DELTA
Optional BRTI delta < $ adds an AND condition to the existing stop-loss checks.
Leave blank to disable; signed finite values including zero are supported.
Delta is BRTI minus target for held UP, target minus BRTI for held DOWN.
Equality does not trigger. Missing/stale BRTI blocks this stop-loss condition.
The condition is checked again in review and saved in the stop-loss snapshot.
Normal close rules remain independent.

OPTIONAL BUY BRTI DISTANCE
Leave a fallback rule's BRTI delta > $ blank to disable its distance condition.
Blank is persisted as null and permits either side of target, including equality.
Zero remains an active strict threshold. Live direction, fresh BRTI/target data,
and the remaining purchase checks are still required.

TARGET CALENDAR DATE VALIDATION
Clock times alone cannot identify a round. The reader now verifies About's
explicit before-time/date endpoints against the exact app start/end, including
timezone and midnight crossings. When About endpoints are absent, a matching
Timeline Event day is required as well as the heading times. Missing or old
dates block target acceptance and retry current-event navigation.

ROLLING 60-SECOND UNDERLYING RANGE
The chart displays a volatility indicator defined as the highest minus lowest
observed source price over the trailing 60 seconds, in USD and as a percentage
of the window low. This is observed range, not directional change or a forecast.
Each new source point stores volatility_60s in its per-round JSONL record.
The live chart includes a range timeline and inspection labels; past round
charts restore recorded metrics. Old records without this field show no metric.
At least 60 seconds of round collection, four points, 50 seconds of window
coverage and no gaps over 20 seconds are required. Missing windows are unavailable;
source points older than 20 seconds are labelled stale in the live indicator.
The window starts fresh each round. No trading conditions are changed.

ROLLING DIRECTION REVERSALS
Each volatility_60s record also includes direction_reversals, up_moves and
down_moves. Reversals count sign changes between consecutive nonzero observed
price moves inside the trailing 60-second window; flat samples are ignored.
An initial move is not a reversal, and no direction is carried from outside
the window. The same coverage/staleness rules as the range indicator apply.
Live text, a separate count timeline and chart inspection show the count;
round files preserve it and restore it from the saved price observations.
Counts reflect sampled source prices, not every market tick.

CLOSE-CONDITIONS BRTI DELTA
Close all includes an optional signed BRTI delta < $ threshold. Blank disables
this extra condition; zero is active. For held UP, delta is BRTI minus target;
for held DOWN, target minus BRTI. Equality does not qualify. A fresh BRTI quote
is required when configured, even with fallback buying disabled. This is an
AND condition with the normal close price/window checks, rechecked in review
and persisted with the close order conditions. Stop loss remains independent.

COLLAPSIBLE VOLATILITY CHARTS
Click each rolling-range or direction-reversals chart heading to independently
hide/show its plot. Choices persist in this browser across live refreshes and
page reloads, and also apply to past-round charts. Recording continues while hidden.

RECORDED-ROUND LIVE SUPPORT
The existing Live probability already uses current price versus target and time
remaining. A new residual logistic calibration layer also uses observed trailing
60-second range, normalized target gap, up/down imbalance, reversal rate, minute
momentum and round progress. This supports Live only; the frozen Final is unchanged.

Each round rebuilds from up to 300 prior completed rounds with recorded source
prices, frozen p_up and usable settlement labels. Examples are sampled near
minutes 2, 5, 8, 11 and 14, using only source points available at each timestamp.
The original Live baseline is replayed with pre-round candle history. Older
observations can supply derived indicators even when no metric was originally
stored. Missing windows, invalid labels, large inconsistent targets and settlement
values outside recorded final-minute prices are excluded. No purchase is required.

At least 40 usable rounds are required. The oldest 70% train the regularized
residual model, and the newest 30% validate it. Each round has equal total weight.
The fixed candidate blends 25% learned probability with 75% baseline. Activation
requires at least 10 validation rounds, Brier improvement greater than 0.002,
lower log loss and no decrease in direction accuracy. The model is not refit
on the validation portion. Otherwise status is shadow, or insufficient, and
the original probability/strength/direction remain intact. Missing/stale live
windows also preserve the original recommendation. Thresholds were set before
reviewing the actual-data evaluation; no holdout tuning is performed.

The Live card displays support status, usable rounds and validation scores.
Every new Live refresh logs baseline/candidate/applied probabilities and window
metrics in price_history/<asset>/<round timestamp>.live.jsonl. Purchase snapshots
also retain this support information. These logs enable prospective evaluation.
Historical reconstruction uses currently available candle history and sampled
settlement labels, not a pristine archive of historical Live outputs. Holdout
results are exploratory, not an execution/P&L test or proof of future gains.
Existing buy/sell filters and source freshness checks remain in effect.


Long-running server performance
------------------------------
Round journals keep the same atomic JSON storage, with compact encoding and
no-op writes skipped. A bounded, file-signature-aware cache avoids reparsing the
journal for each current-round lookup. Per-round get() copies only that round;
statistics use metadata without copying chart histories. External file changes
invalidate cached snapshots, and returned records cannot mutate cached data.
Completed price-history windows cache their validated derived metrics (32-window
LRU), rather than recomputing 25 historical charts every history request.

Order messages are now archived in order_status.sqlite3. The server retains at
most 500 recent messages in memory; /api/state sends only the latest message ID.
The More sidebar fetches at most 200 messages per page, newest first. Older logs
and Latest navigate the complete archive without growing the browser DOM.
Keep order_status.sqlite3 with your backups if you want to retain these logs.

Large JSON API responses negotiate gzip. Dashboard state polls cannot overlap;
unchanged live chart data and unchanged historical rows reuse their existing DOM.
The rolling BTC/cent calculation uses a moving window without changing the
estimator or its quality gates. Source polling and quote-age limits are unchanged.
Restart the server and refresh browser tabs to load these changes.
Measured local results and methodology are in reports/runtime_performance.json.

SYSTEMD SERVICE (THIS CHECKOUT)
The installed user service is BTCTargetPulse.service. Its source is deploy/BTCTargetPulse.service;
paths in that file target /home/waseem/repo/trading/predict/cpmc1.
It uses .venv/bin/python and runs Chrome headless with the existing Robinhood
profile. The dashboard remains on port 8000. Do not also run app.py manually.

This single service starts the dashboard, live price source, history refresh,
prediction/learning workers, and Robinhood target/order browser together.
The obsolete cpmc-local-price.service and cpmc-local-target.timer are disabled:
they referenced the removed /home/waseem/cpmc checkout and are not dependencies
of this application.

  systemctl --user status BTCTargetPulse.service
  systemctl --user restart BTCTargetPulse.service
  systemctl --user stop BTCTargetPulse.service
  systemctl --user start BTCTargetPulse.service
  journalctl --user -u BTCTargetPulse.service -f

The service is enabled under default.target and user lingering is enabled for
waseem, so it starts at boot without login and survives logout. Restart=always
restarts an exited process after 5 seconds; an explicit systemctl stop keeps it
stopped. To disable automatic startup:

  systemctl --user disable --now BTCTargetPulse.service

After changing the unit file:

  cp deploy/BTCTargetPulse.service ~/.config/systemd/user/BTCTargetPulse.service
  systemctl --user daemon-reload
  systemctl --user restart BTCTargetPulse.service

For manual Robinhood login, stop this service first, run
.venv/bin/python robinhood_target.py --login from a graphical session, close
that browser after login, then start the service again.

LEARNING FROM NEW SETTLED ROUNDS
Frozen Decision now evaluates an additional recorded-round probability calibrator
before publishing each new round. It uses up to 300 past settled rounds for the
same asset, with one example per round and the four recorded model probabilities.
Unsettled, current/future, inconsistent, incomplete, and duplicate rounds do not
inflate the training set. The original frozen probability and the evaluation
summary are journaled as frozen_learning so later training avoids applying the
same calibration repeatedly. Existing frozen decisions are never rewritten.

Live learning now prefers pre-learning probabilities actually recorded in each
round's .live.jsonl log; older missing observations fall back to historical replay.
Only records at or before the sampled observation, within 20 seconds and with a
matching target, qualify. Reports count recorded and replayed examples separately.
The Live learner retrains in the background each round; Frozen trains once when
the round prediction is generated. Both remain independent evaluations.

Both learners require at least 40 usable rounds, reserve the newest 30 percent
for chronological validation (at least 10 rounds), and blend a candidate by 25%.
Activation requires Brier improvement greater than .002, lower log loss, and no
accuracy decrease. Otherwise the candidate remains shadow-only and the baseline
is retained. The dashboard shows learning status and baseline/candidate holdout
accuracy on both cards. Holdout results describe probability/direction accuracy,
not execution quality or trading profit. No improvement is guaranteed.

Force Buy positions never use automatic Close all or Stop loss, even when enabled.
Explicitly requested Force Close remains available.

BTC PRICE UPDATE LATENCY
The shared rendered BRTI page and BTC live worker are checked every 0.5 seconds.
Dashboard state refreshes every 0.5 seconds with overlapping requests prevented.
This reduces local detection/display delay; it does not change how often the
upstream BRTI website publishes a new quote. The 20-second source freshness
limit remains unchanged. HTML fallback requests remain limited to every 2 seconds.

Test mode (virtual trading)
---------------------------
In Trade Conditions, enable Test mode and save the settings. Entries follow the
same primary, fallback, and Force Buy conditions, but no broker order controls
are used. Virtual trades are recorded as filled, marked TEST / Virtual, and
closed under the configured exit conditions or settled at round end. Prices
use displayed contract quotes (the No quote can be estimated), and P/L uses
the existing estimated fees. These are simulated fills, not actual executions.
Virtual holdings remain virtual after disabling Test mode. While Test mode is
on, existing live holdings are not automatically closed by the app.

Test mode permits virtual buys while the Final prediction accuracy filter
pauses real buys. Other entry conditions and the choppiness guard still apply.

Automatic test mode on accuracy pause
When the Final prediction accuracy pause is enabled and its guard pauses real
buys, new entries automatically become virtual even if Test mode is unchecked.
When the guard allows buys again, new entries return to the saved Test mode
setting. Existing virtual positions always close virtually. Other entry gates
remain active. The accuracy status displays Automatic test mode while paused.

BTC entry stability (Trade Conditions)
The optional global "Require BTC on buy side of target" condition defaults to
off, with a 60-second duration. It applies to Primary and every Fallback,
including virtual entries. Force Buy bypasses this stability condition. A buy requires observed BTC prices strictly on the bought
side of the round target for the configured duration. Touching/crossing the
target or a quote gap above 10 seconds resets the timer. The elapsed duration
uses source observation timestamps, not time spent waiting without quotes.
Fresh BRTI on that same side is required; missing data blocks entries while the
condition is enabled. It is rechecked during preparation and before submission.
The condition and duration are saved with each purchase. Existing exits do not
use this gate. This condition does not add a minimum dollar distance.

CRYPTO.COM FIVE-MINUTE BTC COMPANION
Open /crypto-com from the dashboard header. This separate page polls the public
Crypto.com Exchange BTC_USD ticker and contract quotes every second; one-minute candles
refresh once a minute. Exchange prices are explicitly a proxy;
they are not verified Predict settlement or contract quotes. Quotes older than
20 seconds disable prediction. The target is read automatically using a dedicated persistent Chrome profile
(.crypto-com-browser), matching the Robinhood browser approach. The reader verifies
the LIVE heading, dated market link, and countdown against the current five-minute
interval in America/New_York. Automatic targets take priority over manual input,
remain fixed through temporary read failures, and are cleared at rollover.
Manual fallback input is bound to one round. A second tab follows the verified
current market URL and reads the visible Yes/No buttons in cents. Quote target,
URL and round must match; quotes older than 15 seconds are hidden. Target and
both contract prices are shown as separate dashboard cards. The browser starts when the new
page is first polled and uses the application --headless option.
The experimental empirical bootstrap samples contiguous pre-round minute returns
for the remaining horizon; it does not reuse the Robinhood 15-minute prediction
or its accuracy statistics. Probabilities have not been backtested/calibrated.
Public Predict target reading has been verified live in visible and headless
Chrome. Order-form preparation is not implemented; authentication and the form
still need verification. This page never fills or submits orders.
Restart app.py to load the new routes. Tests: python -m unittest tests.test_crypto_predict tests.test_crypto_website
The Crypto.com page shares /dashboard.css with the Robinhood dashboard, including
its compact cards, header countdown and two-column monitor layout. Its chart shows
BTC/USD, target and the Yes contract on separate price scales. The market history
table contains only observations collected while this page is open; it is not
purchase history or settled-round accuracy. Chart lines can be toggled separately.

IOC ZERO-FILL RETRY
After a saved IOC buy has waited at least 10 seconds, the browser checks for a
terminal canceled/expired/rejected receipt with Filled quantity 0 of the saved
quantity, matching contract side, target and current round. A full fill takes
priority. Once zero-fill cancellation is verified, the browser refreshes and
archives/removes the app purchase record (the same retry eligibility as the X
control), retaining its details. The normal entry conditions are evaluated again.
Elapsed time alone never proves cancellation: uncertain receipts, partial fills,
GTD orders and virtual orders are not cleared by this rule. Failed refreshes keep
the purchase blocking retries until the refresh succeeds. Attempt/submission
identity is rechecked by the backend before either state transition.
