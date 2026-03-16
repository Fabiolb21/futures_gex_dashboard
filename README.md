# 📈 Futures GEX Dashboard

Real-time **Gamma Exposure (GEX)** tracking for **Futures Options** via Tastytrade / dxFeed.

## Supported Contracts

| Ticker | Name | Category | Multiplier | Option Prefix |
|--------|------|----------|-----------|--------------|
| `ES1!` | E-mini S&P 500 | Equity | $50/pt | `EW` |
| `NQ1!` | E-mini Nasdaq 100 | Equity | $20/pt | `NQ` |
| `CL1!` | Crude Oil WTI | Energy | 1,000 bbl | `LO` |
| `HG1!` | Copper | Metals | 25,000 lb | `HXE` |
| `NG1!` | Natural Gas | Energy | 10,000 MMBtu | `ON` |
| `GC1!` | Gold | Metals | 100 oz | `OG` |

> **Note on option prefixes:** dxFeed uses specific prefixes for futures options.
> If a prefix doesn't return data, try the alt prefix shown in the sidebar info card
> (e.g. try `ES` if `EW` returns nothing for E-mini S&P 500).

## Quick Start

### Local

```bash
pip install -r requirements.txt

# Create .env with your Tastytrade credentials:
# CLIENT_ID=...
# CLIENT_SECRET=...
# REFRESH_TOKEN=...

streamlit run app.py
```

### Streamlit Cloud

1. Push to GitHub
2. Deploy on [share.streamlit.io](https://share.streamlit.io)
3. Add secrets in Advanced Settings:

```toml
CLIENT_ID = "your_client_id"
CLIENT_SECRET = "your_client_secret"
REFRESH_TOKEN = "your_refresh_token"
```

## How GEX is Calculated

```
GEX = Gamma × Open Interest × Contract Multiplier × Futures Price
Net GEX = Call GEX − Put GEX
Zero Gamma = Strike where Net GEX crosses zero (interpolated)
```

## Key Differences vs Equity GEX Dashboard

- **Price format**: supports decimals (e.g. NG @ 3.25, HG @ 4.20)
- **Strike increments**: commodity-aware (0.1 for NG, 10 for GC, etc.)
- **dxFeed symbols**: uses `/ES`, `/NQ`, `/CL`, etc. for front-month price
- **Option prefixes**: futures options have different prefixes from equity options
- **Dark theme**: terminal-style UI optimised for trading screens

## Project Structure

```
futures_gex_dashboard/
├── app.py               # Main Streamlit app
├── requirements.txt
├── README.md
└── utils/
    ├── __init__.py
    ├── auth.py          # Tastytrade OAuth (shared)
    ├── gex_calculator.py
    └── websocket_manager.py
```

## Troubleshooting

**No data returned?**
- Try a different option prefix in the sidebar (the field is editable)
- Ensure you selected the correct expiration date
- Futures options may have less liquidity; try wider strike ranges

**Price fetch fails?**
- dxFeed may use a different symbol format; app falls back to default price
- Market may be closed; the default price is used automatically

---

⚠️ *Educational purposes only. Futures trading involves significant risk.*
