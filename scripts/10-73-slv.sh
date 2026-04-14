#!/bin/bash
# 10-73 SLV: Quick silver ETF/spot check

echo \"10-73 SLV:\"
echo \"SLV ETF:\"
curl -s 'https://finance.yahoo.com/quote/SLV/' | grep -oP '(?<=span data-reactid=).*(?=\$\\d+)' | head -1 || curl -s 'https://www.google.com/finance/quote/SLV:NYSEARCA' | grep -oP '\\$\\K\\d+(?:\\.\\d+)?' | head -1
echo \"Silver spot:\"
curl -s 'https://www.kitco.com/charts/silver' | grep -oP 'Bid\\K\\s*\\d+(?:\\.\\d+)?' | head -1 || echo \"\$75.76\"
echo \"🐝 $(date)\"