#!/usr/bin/env bash
# Usage: quote.sh AAPL  |  quote.sh 0700.HK  |  quote.sh 02382.HK
set -e
SYM="${1:?usage: quote.sh TICKER}"
cd "/data/qbao775/AlphaTrader/backend" 2>/dev/null || { echo "AlphaTrader missing"; exit 1; }
conda run -n alphatrader python3 -c "
import sys; sys.path.insert(0,'.')
import market_data as md
q = md.get_stock_quote('$SYM')
if q:
    print(f\"$SYM: \${q.get('current')} ({q.get('change_pct',0):+.2f}% today) | hi/lo \${q.get('high','-')}/\${q.get('low','-')} | vol {q.get('volume','-'):,}\")
else:
    print(f'$SYM: no data')
" 2>/dev/null
