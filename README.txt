Crypto Prediction Markets Companion for Robinhood

python -m pip install -r requirements.txt
python app.py

The dashboard:
- supports BTC, ETH, SOL, XRP, DOGE, BNB, and HYPE with an in-page asset selector
- keeps separate accuracy statistics for every asset
- lets you enter the Robinhood target in the browser
- freezes the original prediction for the current 15-minute round
- updates a separate live recommendation once per minute and says whether
  it is still the same or has changed based on the live price, the move from
  round start, and the remaining time
- updates the live multi-exchange proxy automatically
- shows UP/DOWN probabilities, model weights, backtest, exchange inputs
- records proxy HIT/MISS at the next round boundary

IMPORTANT:
This is a free multi-exchange USD price proxy.
The platform's official settlement can differ.
Independent companion tool; not affiliated with or endorsed by Robinhood.

TERMINAL EXAMPLES
python btc.py --asset BTC
python btc.py --asset ETH
python btc.py --asset SOL
python btc.py --asset XRP
python btc.py --asset DOGE
python btc.py --asset BNB
python btc.py --asset HYPE
