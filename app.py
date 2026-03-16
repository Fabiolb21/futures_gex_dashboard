"""
Futures GEX Dashboard
Real-time Gamma Exposure for Futures Options via Tastytrade API.
Uses REST API to get correct option chain + streamer symbols, then subscribes via dxFeed WebSocket.
Supports: ES1!, NQ1!, CL1!, HG1!, NG1!, GC1!
"""
import streamlit as st
import json
import time
import math
import requests
from datetime import datetime, timedelta
from websocket import create_connection
import pandas as pd
import plotly.graph_objects as go
from utils.auth import get_access_token, ensure_streamer_token, get_streamer_token


def get_fresh_tokens():
    """
    Fetch access token + streamer token + websocket URL in a single flow.
    Returns (access_token, streamer_token, websocket_url).
    
    CRITICAL: The /api-quote-tokens endpoint returns a session-specific
    websocket-url that MUST be used — the hardcoded URL causes
    'Session not found' errors.
    """
    from utils.auth import load_credentials_from_env

    # Step 1: Get access token via OAuth refresh flow
    creds = load_credentials_from_env()
    resp = requests.post(
        "https://api.tastytrade.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
        timeout=15
    )
    if resp.status_code != 200:
        raise Exception(f"Falha ao obter access token: {resp.status_code} {resp.text[:200]}")

    access_token = resp.json()["access_token"]

    # Step 2: Get streamer token + websocket URL
    resp2 = requests.get(
        "https://api.tastyworks.com/api-quote-tokens",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15
    )
    if resp2.status_code != 200:
        raise Exception(f"Falha ao obter streamer token: {resp2.status_code} {resp2.text[:200]}")

    data = resp2.json().get("data", {})
    streamer_token = data.get("token")
    # The API returns the exact WebSocket URL to use for this session
    websocket_url = data.get("websocket-url") or data.get("dxlink-url") or DXFEED_URL

    if not streamer_token:
        raise Exception(f"Streamer token não encontrado: {resp2.json()}")

    # Ensure WSS protocol
    if websocket_url and not websocket_url.startswith("wss://"):
        websocket_url = websocket_url.replace("https://", "wss://").replace("http://", "wss://")

    return access_token, streamer_token, websocket_url
from utils.gex_calculator import GEXCalculator

st.set_page_config(
    page_title="Futures GEX Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Space+Grotesk:wght@300;400;600&display=swap');
html, body, [class*="css"] { font-family: 'Space Grotesk', sans-serif; }
.stMetric label {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.72rem !important;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #8899aa !important;
}
</style>
""", unsafe_allow_html=True)

TASTYTRADE_API = "https://api.tastytrade.com"

# Futures config: maps UI ticker -> Tastytrade futures symbol
FUTURES_CONFIG = {
    "ES1!": {
        "label": "E-mini S&P 500",
        "category": "equity",
        "tasty_symbol": "/ES",        # Tastytrade REST symbol
        "default_price": 5800,
        "increment": 5,
        "multiplier": 50,
        "currency": "USD",
        "emoji": "📊",
        "color": "#00aaff",
    },
    "NQ1!": {
        "label": "E-mini Nasdaq 100",
        "category": "equity",
        "tasty_symbol": "/NQ",
        "default_price": 21000,
        "increment": 25,
        "multiplier": 20,
        "currency": "USD",
        "emoji": "💻",
        "color": "#00aaff",
    },
    "CL1!": {
        "label": "Crude Oil WTI",
        "category": "energy",
        "tasty_symbol": "/CL",
        "default_price": 75,
        "increment": 1,
        "multiplier": 1000,
        "currency": "USD/bbl",
        "emoji": "🛢️",
        "color": "#ff6600",
    },
    "HG1!": {
        "label": "Copper",
        "category": "metals",
        "tasty_symbol": "/HG",
        "default_price": 450,
        "increment": 5,
        "multiplier": 25000,
        "currency": "USD/lb",
        "emoji": "🔶",
        "color": "#cc6633",
    },
    "NG1!": {
        "label": "Natural Gas",
        "category": "energy",
        "tasty_symbol": "/NG",
        "default_price": 3,
        "increment": 0.1,
        "multiplier": 10000,
        "currency": "USD/MMBtu",
        "emoji": "🔥",
        "color": "#ff6600",
    },
    "GC1!": {
        "label": "Gold",
        "category": "metals",
        "tasty_symbol": "/GC",
        "default_price": 3100,
        "increment": 10,
        "multiplier": 100,
        "currency": "USD/oz",
        "emoji": "🥇",
        "color": "#ffcc00",
    },
}

CATEGORY_LABELS = {
    "equity": "📊 Equity",
    "energy": "⚡ Energy",
    "metals": "🥇 Metals"
}

DXFEED_URL = "wss://tasty-openapi-ws.dxfeed.com/realtime"


# ── REST API helpers ──────────────────────────────────────

def get_futures_option_chain(access_token, tasty_symbol):
    """
    Fetch the full futures option chain from Tastytrade REST API.
    Endpoint: GET /futures-option-chains/{contract_code}/nested
    contract_code = "ES", "NQ", "CL", etc. (no leading slash)
    
    Returns dict: {expiration_date_str: [option_dicts]}
    Each option: {streamer_symbol, strike, type ("C"/"P"), expiration}
    
    Real response example for a strike:
    {
      "strike-price": "5750.0",
      "call": "./ESU4 EW4Q4 240823C5750",
      "call-streamer-symbol": "./EW4Q24C5750:XCME",
      "put": "./ESU4 EW4Q4 240823P5750",
      "put-streamer-symbol": "./EW4Q24P5750:XCME"
    }
    """
    contract_code = tasty_symbol.lstrip("/")  # "ES", "NQ", etc.
    url = f"{TASTYTRADE_API}/futures-option-chains/{contract_code}/nested"
    headers = {"Authorization": f"Bearer {access_token}"}

    resp = requests.get(url, headers=headers, timeout=20)
    if resp.status_code != 200:
        raise Exception(
            f"Erro ao buscar option chain ({resp.status_code}):\n"
            f"URL: {url}\n"
            f"Resposta: {resp.text[:500]}"
        )

    body = resp.json()
    # body["data"]["future-option-chains"] is a list of chain objects
    # each has "expirations" list, each expiration has "strikes" list
    data = body.get("data", {})
    
    expirations = {}

    # The API returns TWO relevant keys:
    # 1. "futures"       -> list of contract months (e.g. /ESH6, /ESM6)
    # 2. "option-chains" -> list of option chain objects, each with "expirations"
    # We try both key names for safety
    chain_list = (
        data.get("option-chains") or
        data.get("future-option-chains") or
        []
    )

    for chain_obj in chain_list:
        for exp_obj in chain_obj.get("expirations", []):
            exp_date = exp_obj.get("expiration-date", "")
            if not exp_date:
                continue
            options = []
            for strike_obj in exp_obj.get("strikes", []):
                try:
                    strike_price = float(strike_obj.get("strike-price", 0))
                except (ValueError, TypeError):
                    continue

                # Keys can be "call-streamer-symbol" / "put-streamer-symbol"
                # OR just nested under "call" / "put" dicts
                call_sym = strike_obj.get("call-streamer-symbol")
                put_sym  = strike_obj.get("put-streamer-symbol")

                # Fallback: sometimes nested as dict with "streamer-symbol" key
                if not call_sym and isinstance(strike_obj.get("call"), dict):
                    call_sym = strike_obj["call"].get("streamer-symbol")
                if not put_sym and isinstance(strike_obj.get("put"), dict):
                    put_sym = strike_obj["put"].get("streamer-symbol")

                if call_sym:
                    options.append({
                        "streamer_symbol": call_sym,
                        "strike": strike_price,
                        "type": "C",
                        "expiration": exp_date,
                    })
                if put_sym:
                    options.append({
                        "streamer_symbol": put_sym,
                        "strike": strike_price,
                        "type": "P",
                        "expiration": exp_date,
                    })

            if options:
                if exp_date not in expirations:
                    expirations[exp_date] = options
                else:
                    expirations[exp_date].extend(options)

    return expirations


def parse_option_chain(chain_data):
    """Legacy shim — chain_data is already the parsed expirations dict."""
    return chain_data


def get_futures_price_rest(access_token, tasty_symbol):
    """
    Get current front-month futures price via Tastytrade REST API.
    Uses the /futures endpoint which returns mark/last price for each contract month.
    """
    contract_code = tasty_symbol.lstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    
    try:
        # Get all contract months for this root symbol
        url = f"{TASTYTRADE_API}/futures-option-chains/{contract_code}/nested"
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            body = resp.json().get("data", {})
            futures_list = body.get("futures", [])
            
            # Find the active month contract
            active = next((f for f in futures_list if f.get("active-month")), None)
            if not active:
                # Fall back to nearest expiration
                from datetime import datetime
                today = datetime.now().date()
                future_contracts = [
                    f for f in futures_list
                    if f.get("expiration-date", "") >= str(today)
                ]
                active = min(future_contracts, key=lambda f: f["expiration-date"]) if future_contracts else None

            if active:
                # Use the contract symbol to get its price
                contract_sym = active.get("symbol", "")  # e.g. "/ESH6"
                sym_encoded = contract_sym.replace("/", "%2F")
                price_url = f"{TASTYTRADE_API}/futures/{sym_encoded}"
                price_resp = requests.get(price_url, headers=headers, timeout=10)
                if price_resp.status_code == 200:
                    pdata = price_resp.json().get("data", {})
                    price = pdata.get("mark") or pdata.get("last-price") or pdata.get("mark-price")
                    if price:
                        return float(price)
    except Exception:
        pass
    return None


def get_active_streamer_symbol(access_token, tasty_symbol):
    """
    Get the dxFeed streamer symbol for the active futures contract month.
    e.g. "/ESH26:XCME" for the active E-mini S&P 500 contract.
    """
    contract_code = tasty_symbol.lstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        url = f"{TASTYTRADE_API}/futures-option-chains/{contract_code}/nested"
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            futures_list = resp.json().get("data", {}).get("futures", [])
            active = next((f for f in futures_list if f.get("active-month")), None)
            if not active:
                # Use next-active-month as fallback
                active = next((f for f in futures_list if f.get("next-active-month")), None)
            if active:
                return active.get("streamer-symbol")  # e.g. "/ESH26:XCME"
    except Exception:
        pass
    return None


# ── WebSocket helpers ─────────────────────────────────────

def connect_websocket(token, url=None):
    """
    Connect and authenticate with dxFeed WebSocket.
    Uses the session-specific URL returned by /api-quote-tokens when available.
    """
    ws = create_connection(url or DXFEED_URL, timeout=20)

    # SETUP
    ws.send(json.dumps({
        "type": "SETUP", "channel": 0,
        "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60, "version": "1.0.0"
    }))

    # Wait for server SETUP ack, then AUTH_STATE(UNAUTHORIZED), then send AUTH
    # Exactly like the original working app
    deadline = time.time() + 30
    authorized = False

    while time.time() < deadline:
        try:
            ws.settimeout(3)
            raw = ws.recv()
            if not raw or not raw.strip():
                continue
            msg = json.loads(raw)
        except ValueError:
            continue
        except Exception:
            continue

        mtype = msg.get("type", "")

        if mtype == "AUTH_STATE":
            state = msg.get("state", "")
            if state == "UNAUTHORIZED":
                # Only send AUTH when server explicitly asks — exactly like original app
                ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": token}))
            elif state == "AUTHORIZED":
                authorized = True
                break

        elif mtype == "KEEPALIVE":
            ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))

        elif mtype == "ERROR":
            err_msg = msg.get("message", str(msg))
            # Log full message to help debug
            raise Exception(f"dxFeed error: {err_msg} | full_msg={json.dumps(msg)}")

    if not authorized:
        raise Exception("dxFeed WebSocket: timeout na autenticação (30s)")

    # Request FEED channel
    ws.send(json.dumps({
        "type": "CHANNEL_REQUEST", "channel": 1,
        "service": "FEED", "parameters": {"contract": "AUTO"}
    }))

    # Wait for CHANNEL_OPENED
    deadline2 = time.time() + 15
    while time.time() < deadline2:
        try:
            ws.settimeout(3)
            raw = ws.recv()
            if not raw or not raw.strip():
                continue
            msg = json.loads(raw)
        except Exception:
            continue

        mtype = msg.get("type", "")
        if mtype == "CHANNEL_OPENED" and msg.get("channel") == 1:
            break
        if mtype == "KEEPALIVE":
            ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))

    return ws


def get_futures_option_chain(access_token, tasty_symbol):
    """
    Fetch the full futures option chain from Tastytrade REST API.
    Endpoint: GET /futures-option-chains/{contract_code}/nested
    contract_code = "ES", "NQ", "CL", etc. (no leading slash)
    
    Returns dict: {expiration_date_str: [option_dicts]}
    Each option: {streamer_symbol, strike, type ("C"/"P"), expiration}
    
    Real response example for a strike:
    {
      "strike-price": "5750.0",
      "call": "./ESU4 EW4Q4 240823C5750",
      "call-streamer-symbol": "./EW4Q24C5750:XCME",
      "put": "./ESU4 EW4Q4 240823P5750",
      "put-streamer-symbol": "./EW4Q24P5750:XCME"
    }
    """
    contract_code = tasty_symbol.lstrip("/")  # "ES", "NQ", etc.
    url = f"{TASTYTRADE_API}/futures-option-chains/{contract_code}/nested"
    headers = {"Authorization": f"Bearer {access_token}"}

    resp = requests.get(url, headers=headers, timeout=20)
    if resp.status_code != 200:
        raise Exception(
            f"Erro ao buscar option chain ({resp.status_code}):\n"
            f"URL: {url}\n"
            f"Resposta: {resp.text[:500]}"
        )

    body = resp.json()
    # body["data"]["future-option-chains"] is a list of chain objects
    # each has "expirations" list, each expiration has "strikes" list
    data = body.get("data", {})
    
    expirations = {}

    # The API returns TWO relevant keys:
    # 1. "futures"       -> list of contract months (e.g. /ESH6, /ESM6)
    # 2. "option-chains" -> list of option chain objects, each with "expirations"
    # We try both key names for safety
    chain_list = (
        data.get("option-chains") or
        data.get("future-option-chains") or
        []
    )

    for chain_obj in chain_list:
        for exp_obj in chain_obj.get("expirations", []):
            exp_date = exp_obj.get("expiration-date", "")
            if not exp_date:
                continue
            options = []
            for strike_obj in exp_obj.get("strikes", []):
                try:
                    strike_price = float(strike_obj.get("strike-price", 0))
                except (ValueError, TypeError):
                    continue

                # Keys can be "call-streamer-symbol" / "put-streamer-symbol"
                # OR just nested under "call" / "put" dicts
                call_sym = strike_obj.get("call-streamer-symbol")
                put_sym  = strike_obj.get("put-streamer-symbol")

                # Fallback: sometimes nested as dict with "streamer-symbol" key
                if not call_sym and isinstance(strike_obj.get("call"), dict):
                    call_sym = strike_obj["call"].get("streamer-symbol")
                if not put_sym and isinstance(strike_obj.get("put"), dict):
                    put_sym = strike_obj["put"].get("streamer-symbol")

                if call_sym:
                    options.append({
                        "streamer_symbol": call_sym,
                        "strike": strike_price,
                        "type": "C",
                        "expiration": exp_date,
                    })
                if put_sym:
                    options.append({
                        "streamer_symbol": put_sym,
                        "strike": strike_price,
                        "type": "P",
                        "expiration": exp_date,
                    })

            if options:
                if exp_date not in expirations:
                    expirations[exp_date] = options
                else:
                    expirations[exp_date].extend(options)

    return expirations


def parse_option_chain(chain_data):
    """Legacy shim — chain_data is already the parsed expirations dict."""
    return chain_data


def get_futures_price_rest(access_token, tasty_symbol):
    """
    Get current front-month futures price via Tastytrade REST API.
    Uses the /futures endpoint which returns mark/last price for each contract month.
    """
    contract_code = tasty_symbol.lstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    
    try:
        # Get all contract months for this root symbol
        url = f"{TASTYTRADE_API}/futures-option-chains/{contract_code}/nested"
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            body = resp.json().get("data", {})
            futures_list = body.get("futures", [])
            
            # Find the active month contract
            active = next((f for f in futures_list if f.get("active-month")), None)
            if not active:
                # Fall back to nearest expiration
                from datetime import datetime
                today = datetime.now().date()
                future_contracts = [
                    f for f in futures_list
                    if f.get("expiration-date", "") >= str(today)
                ]
                active = min(future_contracts, key=lambda f: f["expiration-date"]) if future_contracts else None

            if active:
                # Use the contract symbol to get its price
                contract_sym = active.get("symbol", "")  # e.g. "/ESH6"
                sym_encoded = contract_sym.replace("/", "%2F")
                price_url = f"{TASTYTRADE_API}/futures/{sym_encoded}"
                price_resp = requests.get(price_url, headers=headers, timeout=10)
                if price_resp.status_code == 200:
                    pdata = price_resp.json().get("data", {})
                    price = pdata.get("mark") or pdata.get("last-price") or pdata.get("mark-price")
                    if price:
                        return float(price)
    except Exception:
        pass
    return None


def get_active_streamer_symbol(access_token, tasty_symbol):
    """
    Get the dxFeed streamer symbol for the active futures contract month.
    e.g. "/ESH26:XCME" for the active E-mini S&P 500 contract.
    """
    contract_code = tasty_symbol.lstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        url = f"{TASTYTRADE_API}/futures-option-chains/{contract_code}/nested"
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            futures_list = resp.json().get("data", {}).get("futures", [])
            active = next((f for f in futures_list if f.get("active-month")), None)
            if not active:
                # Use next-active-month as fallback
                active = next((f for f in futures_list if f.get("next-active-month")), None)
            if active:
                return active.get("streamer-symbol")  # e.g. "/ESH26:XCME"
    except Exception:
        pass
    return None


# ── WebSocket helpers ─────────────────────────────────────

def connect_websocket(token):
    """
    Connect and authenticate with dxFeed WebSocket.
    Handles empty frames, keepalives, and out-of-order messages gracefully.
    """
    ws = create_connection(DXFEED_URL, timeout=20)

    # SETUP
    ws.send(json.dumps({
        "type": "SETUP", "channel": 0,
        "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60, "version": "1.0.0"
    }))

    # Send AUTH immediately after SETUP — dxFeed expects this flow:
    # Client: SETUP -> Server: SETUP -> Server: AUTH_STATE(UNAUTHORIZED)
    # Client: AUTH  -> Server: AUTH_STATE(AUTHORIZED)
    # But sometimes AUTH_STATE arrives before we loop, so we send AUTH proactively too.

    auth_sent = False
    authorized = False
    deadline = time.time() + 30  # 30 second timeout

    while time.time() < deadline:
        try:
            ws.settimeout(2)
            raw = ws.recv()
            if not raw:
                continue
            msg = json.loads(raw)
        except Exception:
            continue

        mtype = msg.get("type")

        if mtype == "SETUP":
            # Server acknowledged our SETUP — now send AUTH proactively
            if not auth_sent:
                ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": token}))
                auth_sent = True

        elif mtype == "AUTH_STATE":
            state = msg.get("state")
            if state == "UNAUTHORIZED":
                # Send AUTH (or resend if needed)
                ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": token}))
                auth_sent = True
            elif state == "AUTHORIZED":
                authorized = True
                break

        elif mtype == "KEEPALIVE":
            ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))

        elif mtype == "ERROR":
            raise Exception(f"dxFeed error: {msg.get('message', msg)}")

    if not authorized:
        raise Exception(
            "dxFeed WebSocket: falha na autenticação. "
            "O streamer token pode ter expirado — tente recarregar o app."
        )

    # Request FEED channel
    ws.send(json.dumps({
        "type": "CHANNEL_REQUEST", "channel": 1,
        "service": "FEED", "parameters": {"contract": "AUTO"}
    }))

    # Wait for CHANNEL_OPENED
    deadline2 = time.time() + 15
    while time.time() < deadline2:
        try:
            ws.settimeout(2)
            raw = ws.recv()
            if not raw:
                continue
            msg = json.loads(raw)
        except Exception:
            continue

        mtype = msg.get("type")
        if mtype == "CHANNEL_OPENED" and msg.get("channel") == 1:
            break
        if mtype == "KEEPALIVE":
            ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
        if mtype == "AUTH_STATE" and msg.get("state") == "AUTHORIZED":
            # Sometimes AUTH confirmation arrives after CHANNEL_REQUEST
            ws.send(json.dumps({
                "type": "CHANNEL_REQUEST", "channel": 1,
                "service": "FEED", "parameters": {"contract": "AUTO"}
            }))

    return ws


def fetch_greeks_for_options(ws, options, wait_seconds=20):
    """
    Subscribe to Greeks, Summary, Trade for given options list.
    options: list of dicts with at least "streamer_symbol"
    Returns dict: streamer_symbol -> {gamma, delta, iv, oi, volume, strike, type}
    """
    symbols = [o["streamer_symbol"] for o in options]
    
    subscriptions = []
    for sym in symbols:
        subscriptions += [
            {"symbol": sym, "type": "Greeks"},
            {"symbol": sym, "type": "Summary"},
            {"symbol": sym, "type": "Trade"},
        ]

    # Subscribe in batches of 200 to avoid WebSocket frame issues
    BATCH = 200
    for i in range(0, len(subscriptions), BATCH):
        ws.send(json.dumps({
            "type": "FEED_SUBSCRIPTION",
            "channel": 1,
            "add": subscriptions[i:i+BATCH]
        }))

    # Build lookup: streamer_symbol -> option info
    sym_info = {o["streamer_symbol"]: o for o in options}
    data = {}
    start = time.time()

    while time.time() - start < wait_seconds:
        try:
            ws.settimeout(0.5)
            raw = ws.recv()
            if not raw:
                continue
            msg = json.loads(raw)

            mtype = msg.get("type")

            # Respond to keepalives so connection stays alive during long fetches
            if mtype == "KEEPALIVE":
                ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
                continue

            if mtype != "FEED_DATA":
                continue

            for item in msg.get("data", []):
                sym = item.get("eventSymbol")
                if sym not in sym_info:
                    continue
                if sym not in data:
                    data[sym] = {
                        "strike": sym_info[sym]["strike"],
                        "type": sym_info[sym]["type"],
                        "expiration": sym_info[sym].get("expiration", ""),
                    }
                etype = item.get("eventType")
                if etype == "Greeks":
                    data[sym]["gamma"] = item.get("gamma")
                    data[sym]["delta"] = item.get("delta")
                    data[sym]["iv"] = item.get("volatility")
                elif etype == "Summary":
                    data[sym]["oi"] = item.get("openInterest")
                elif etype == "Trade":
                    data[sym]["volume"] = item.get("dayVolume", 0)

        except ValueError:
            # Empty or malformed frame — skip
            continue
        except Exception:
            continue

    return data


def aggregate_by_strike(option_data):
    """Aggregate by strike from fetched option data dict."""
    strike_data = {}

    for sym, d in option_data.items():
        strike = d.get("strike")
        opt_type = d.get("type")
        if strike is None or opt_type is None:
            continue

        if strike not in strike_data:
            strike_data[strike] = {
                "call_oi": 0, "put_oi": 0,
                "call_volume": 0, "put_volume": 0,
                "call_iv": None, "put_iv": None,
            }

        def sf(val):
            try:
                v = float(val or 0)
                return 0 if math.isnan(v) else v
            except Exception:
                return 0

        oi = sf(d.get("oi", 0))
        vol = sf(d.get("volume", 0))
        iv = d.get("iv")

        if opt_type == "C":
            strike_data[strike]["call_oi"] += oi
            strike_data[strike]["call_volume"] += vol
            if iv is not None:
                try:
                    iv_f = float(iv)
                    if not math.isnan(iv_f) and iv_f > 0:
                        strike_data[strike]["call_iv"] = iv_f
                except Exception:
                    pass
        else:
            strike_data[strike]["put_oi"] += oi
            strike_data[strike]["put_volume"] += vol
            if iv is not None:
                try:
                    iv_f = float(iv)
                    if not math.isnan(iv_f) and iv_f > 0:
                        strike_data[strike]["put_iv"] = iv_f
                except Exception:
                    pass

    if not strike_data:
        return pd.DataFrame(columns=[
            "strike","call_oi","put_oi","call_volume","put_volume",
            "call_iv","put_iv","total_oi","total_volume"
        ])

    return pd.DataFrame([
        {
            "strike": s,
            "call_oi": d["call_oi"], "put_oi": d["put_oi"],
            "call_volume": d["call_volume"], "put_volume": d["put_volume"],
            "call_iv": d["call_iv"], "put_iv": d["put_iv"],
            "total_oi": d["call_oi"] + d["put_oi"],
            "total_volume": d["call_volume"] + d["put_volume"],
        }
        for s, d in strike_data.items()
    ]).sort_values("strike").reset_index(drop=True)


# ── Main ──────────────────────────────────────────────────

def main():
    st.markdown("""
    <div style="display:flex;align-items:center;gap:16px;margin-bottom:4px;">
        <span style="font-size:2rem;">📈</span>
        <div>
            <h1 style="margin:0;font-family:'JetBrains Mono',monospace;font-size:1.6rem;">
                Futures GEX Dashboard
            </h1>
            <p style="margin:0;color:#8899aa;font-size:0.85rem;">
                Gamma Exposure em tempo real · ES · NQ · CL · HG · NG · GC
            </p>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Session state
    for key, default in [
        ("data_fetched", False),
        ("gex_calculator", None),
        ("underlying_price", None),
        ("option_data", {}),
        ("auto_refresh", False),
        ("volume_view", "Calls vs Puts"),
        ("selected_future", "ES1!"),
        ("expiration", ""),
        ("available_expirations", []),
        ("fetch_timestamp", None),
        ("strike_df", None),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ── Sidebar ──────────────────────────────────────────
    with st.sidebar:
        st.markdown("## ⚙️ Configuração")

        selected = st.selectbox(
            "Contrato",
            list(FUTURES_CONFIG.keys()),
            format_func=lambda x: f"{FUTURES_CONFIG[x]['emoji']}  {x}  —  {FUTURES_CONFIG[x]['label']}",
            index=list(FUTURES_CONFIG.keys()).index(st.session_state.selected_future),
        )
        st.session_state.selected_future = selected
        cfg = FUTURES_CONFIG[selected]

        cat_clr = "#00aaff" if cfg["category"] == "equity" else ("#ff6600" if cfg["category"] == "energy" else "#ffcc00")
        st.markdown(f"""
        <div style="border:1px solid {cat_clr}33;border-radius:8px;padding:12px;background:#0d1117;margin:8px 0;">
            <div style="color:{cat_clr};font-size:0.75rem;font-weight:700;text-transform:uppercase;margin-bottom:8px;">
                {CATEGORY_LABELS[cfg["category"]]}
            </div>
            <div style="font-size:0.8rem;color:#8899aa;">
                Multiplier: <b style="color:#e6edf3;">${cfg["multiplier"]:,}</b> &nbsp;·&nbsp;
                Symbol: <b style="color:#e6edf3;font-family:'JetBrains Mono',monospace;">{cfg["tasty_symbol"]}</b>
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.divider()

        # Step 1: Load option chain via REST API
        st.markdown("**Passo 1 — Carregar Chain**")
        st.caption("Busca expirações disponíveis via API REST da Tastytrade")
        
        load_chain = st.button("📋 Carregar Option Chain", use_container_width=True)
        
        if load_chain:
            with st.spinner(f"Buscando option chain de {cfg['tasty_symbol']}..."):
                try:
                    access_token, _ = get_fresh_tokens()
                    expirations = get_futures_option_chain(access_token, cfg["tasty_symbol"])
                    
                    if not expirations:
                        st.error(
                            f"Nenhuma expiração encontrada para {cfg['tasty_symbol']}. "
                            f"Verifique se sua conta tem habilitação para futuros."
                        )
                        with st.expander("Ver resposta bruta da API"):
                            contract_code = cfg["tasty_symbol"].lstrip("/")
                            url = f"https://api.tastytrade.com/futures-option-chains/{contract_code}/nested"
                            raw = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
                            st.code(f"Status: {raw.status_code}\nURL: {url}\n\n{raw.text[:3000]}")
                    else:
                        st.session_state.available_expirations = sorted(expirations.keys())
                        st.session_state.chain_options = expirations
                        
                        # Get active contract streamer symbol for price fetch via WebSocket
                        active_streamer = get_active_streamer_symbol(access_token, cfg["tasty_symbol"])
                        st.session_state.active_streamer_symbol = active_streamer
                        
                        # Try WebSocket price using the active streamer symbol
                        price = None
                        if active_streamer:
                            try:
                                _, _st, _ws_url = get_fresh_tokens()
                                ws_p = connect_websocket(_st, url=_ws_url)
                                # Try both Trade and Quote for the active contract
                                ws_p.send(json.dumps({
                                    "type": "FEED_SUBSCRIPTION", "channel": 1,
                                    "add": [
                                        {"symbol": active_streamer, "type": "Trade"},
                                        {"symbol": active_streamer, "type": "Quote"},
                                        # Also try without exchange suffix
                                        {"symbol": active_streamer.split(":")[0], "type": "Trade"},
                                        {"symbol": active_streamer.split(":")[0], "type": "Quote"},
                                    ]
                                }))
                                candidates = {active_streamer, active_streamer.split(":")[0]}
                                t0 = time.time()
                                while time.time() - t0 < 10 and not price:
                                    try:
                                        ws_p.settimeout(1)
                                        m = json.loads(ws_p.recv())
                                        if m.get("type") == "FEED_DATA":
                                            for item in m.get("data", []):
                                                if item.get("eventSymbol") not in candidates:
                                                    continue
                                                # Trade price
                                                p = item.get("price")
                                                if p and str(p) not in ("NaN","nan",""):
                                                    try:
                                                        fv = float(p)
                                                        if fv > 0:
                                                            price = fv
                                                            break
                                                    except Exception:
                                                        pass
                                                # Quote midpoint
                                                b = item.get("bidPrice")
                                                a = item.get("askPrice")
                                                if b and a:
                                                    try:
                                                        fv = (float(b) + float(a)) / 2
                                                        if fv > 0:
                                                            price = fv
                                                            break
                                                    except Exception:
                                                        pass
                                    except Exception:
                                        pass
                                ws_p.close()
                            except Exception:
                                pass

                        # Fallback: REST API
                        if not price:
                            price = get_futures_price_rest(access_token, cfg["tasty_symbol"])

                        if price:
                            st.session_state.underlying_price = price

                        n_exp = len(expirations)
                        total_opts = sum(len(v) for v in expirations.values())
                        st.success(f"✅ {n_exp} expirações · {total_opts} opções carregadas!")
                        if price:
                            st.info(f"💰 {active_streamer or cfg['tasty_symbol']}: **{price:,.2f}**")
                        else:
                            st.warning(
                                f"Preço não obtido automaticamente. "
                                f"Insira manualmente abaixo antes de fazer o Fetch."
                            )
                        if active_streamer:
                            st.caption(f"Contrato ativo: {active_streamer}")
                except Exception as e:
                    st.error(f"Erro: {e}")

        # Expiration selector
        if st.session_state.available_expirations:
            st.markdown("**Passo 2 — Selecionar Expiração**")
            exp_options = st.session_state.available_expirations
            
            # Default to nearest expiration
            default_idx = 0
            today = datetime.now().date()
            for i, exp in enumerate(exp_options):
                try:
                    exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                    if exp_date >= today:
                        default_idx = i
                        break
                except Exception:
                    pass

            chosen_exp = st.selectbox(
                "Expiração",
                exp_options,
                index=default_idx,
                label_visibility="collapsed"
            )
            st.session_state.expiration = chosen_exp

            # Count options for this expiration
            opts_for_exp = st.session_state.chain_options.get(chosen_exp, [])
            n_strikes = len(set(o["strike"] for o in opts_for_exp))
            st.caption(f"{len(opts_for_exp)} opções · {n_strikes} strikes")

            # Manual price override
            st.markdown("**💲 Preço do Contrato**")
            current_price = st.session_state.underlying_price or float(cfg["default_price"])
            manual_price = st.number_input(
                "Preço atual",
                min_value=0.01,
                max_value=1_000_000.0,
                value=float(current_price),
                step=float(cfg["increment"]),
                format="%.2f",
                help="Preço obtido automaticamente. Edite se necessário.",
                label_visibility="collapsed"
            )
            if manual_price != current_price:
                st.session_state.underlying_price = manual_price

            # Strike range filter
            st.markdown("**Filtro de Strikes (opcional)**")
            if opts_for_exp:
                all_strikes = sorted(set(o["strike"] for o in opts_for_exp))
                default_price = st.session_state.underlying_price or cfg["default_price"]
                
                strikes_around = st.number_input(
                    "Strikes ao redor do preço (0 = todos)",
                    min_value=0, max_value=200, value=30, step=5
                )
                st.session_state.strikes_around = strikes_around

        st.divider()

        # Fetch duration
        st.markdown("**Duração do Fetch (segundos)**")
        wait_seconds = st.slider("Segundos", min_value=10, max_value=60, value=20, step=5,
                                 label_visibility="collapsed")

        auto_refresh = st.checkbox("🔄 Auto-refresh", value=st.session_state.auto_refresh)
        st.session_state.auto_refresh = auto_refresh

        st.divider()

        # Step 3: Fetch GEX data
        fetch_ready = bool(st.session_state.get("available_expirations") and st.session_state.expiration)
        
        fetch_btn = st.button(
            "⚡ Fetch GEX Data",
            type="primary",
            use_container_width=True,
            disabled=not fetch_ready,
            help="Primeiro carregue a option chain acima" if not fetch_ready else "Buscar dados de GEX"
        )

        if not fetch_ready and not load_chain:
            st.info("👆 Primeiro clique em **Carregar Option Chain**")

        if fetch_btn and fetch_ready:
            exp = st.session_state.expiration
            opts = st.session_state.chain_options.get(exp, [])
            
            # Filter strikes around price
            strikes_around = st.session_state.get("strikes_around", 30)
            if strikes_around > 0 and st.session_state.underlying_price:
                price = st.session_state.underlying_price
                inc = cfg["increment"]
                min_strike = price - strikes_around * inc
                max_strike = price + strikes_around * inc
                opts = [o for o in opts if min_strike <= o["strike"] <= max_strike]

            prog = st.empty()
            with st.spinner(f"Buscando Greeks para {len(opts)} opções..."):
                try:
                    prog.info(f"🔑 Obtendo tokens e URL da sessão...")
                    _, streamer_token, ws_url = get_fresh_tokens()
                    prog.info(f"🔌 Conectando ao dxFeed ({ws_url[:40]}...)...")
                    ws = connect_websocket(streamer_token, url=ws_url)

                    prog.info(f"📊 Coletando Greeks para {len(opts)} opções ({wait_seconds}s)...")
                    option_data = fetch_greeks_for_options(ws, opts, wait_seconds)
                    ws.close()

                    # GEX calculation
                    prog.info("🧮 Calculando GEX...")
                    
                    underlying_price = st.session_state.underlying_price or cfg["default_price"]
                    calc = GEXCalculator(spot_price=underlying_price)

                    for sym, d in option_data.items():
                        gamma = d.get("gamma")
                        oi = d.get("oi")
                        strike = d.get("strike")
                        opt_type = d.get("type")
                        if gamma is not None and oi is not None and strike and opt_type:
                            # Build a synthetic symbol for GEXCalculator
                            # Format: .PREFIX{YYMMDD}{C/P}{STRIKE}
                            try:
                                exp_dt = datetime.strptime(exp, "%Y-%m-%d")
                                exp_str = exp_dt.strftime("%y%m%d")
                            except Exception:
                                exp_str = "250101"
                            prefix = cfg["tasty_symbol"].replace("/", "")
                            s_int = int(strike) if strike == int(strike) else strike
                            synthetic = f".{prefix}{exp_str}{opt_type}{s_int}"
                            calc.update_gamma(synthetic, gamma, oi)

                    options_with_data = sum(
                        1 for d in option_data.values()
                        if d.get("gamma") is not None or d.get("oi") is not None
                    )

                    st.session_state.gex_calculator = calc
                    st.session_state.option_data = option_data
                    st.session_state.data_fetched = True
                    st.session_state.fetch_timestamp = datetime.now()
                    st.session_state.strike_df = aggregate_by_strike(option_data)

                    if options_with_data == 0:
                        prog.warning(
                            f"⚠️ {len(option_data)} opções recebidas mas sem Greeks/OI. "
                            f"O dxFeed pode não ter dados de futuros para sua licença. "
                            f"Tente uma expiração diferente ou contrate um plano com dados de futuros."
                        )
                    else:
                        prog.success(f"✅ {options_with_data}/{len(option_data)} opções com dados!")

                except Exception as e:
                    st.error(f"❌ Erro: {e}")
                    import traceback
                    st.code(traceback.format_exc())
                    st.session_state.data_fetched = False

        if st.session_state.fetch_timestamp:
            st.caption(f"Último fetch: {st.session_state.fetch_timestamp.strftime('%H:%M:%S')}")

    # ── Main content ──────────────────────────────────────
    if not st.session_state.data_fetched:
        st.info("👈 No sidebar: 1) Selecione o contrato → 2) Clique **Carregar Option Chain** → 3) Escolha a expiração → 4) Clique **Fetch GEX Data**")

        cols = st.columns(3)
        futures_by_cat = {
            "equity": [("ES1!", "E-mini S&P 500", "📊"), ("NQ1!", "E-mini Nasdaq 100", "💻")],
            "energy": [("CL1!", "Crude Oil WTI", "🛢️"), ("NG1!", "Natural Gas", "🔥")],
            "metals": [("GC1!", "Gold", "🥇"), ("HG1!", "Copper", "🔶")],
        }
        for col, (cat, items) in zip(cols, futures_by_cat.items()):
            with col:
                cat_clr2 = "#00aaff" if cat == "equity" else ("#ff6600" if cat == "energy" else "#ffcc00")
                lines = "".join(
                    f'<div style="margin-bottom:10px;"><b>{e} {t}</b><br><span style="color:#8899aa;font-size:0.8rem;">{l}</span></div>'
                    for t, l, e in items
                )
                st.markdown(f'''
                <div style="border:1px solid {cat_clr2}22;border-radius:8px;padding:16px;">
                    <div style="color:{cat_clr2};font-weight:700;font-size:0.8rem;text-transform:uppercase;margin-bottom:10px;">
                        {CATEGORY_LABELS[cat]}
                    </div>
                    {lines}
                </div>''', unsafe_allow_html=True)
        return

    # ── Dashboard ─────────────────────────────────────────
    calc = st.session_state.gex_calculator
    metrics = calc.get_total_gex_metrics()
    strike_df = st.session_state.strike_df
    cfg = FUTURES_CONFIG[st.session_state.selected_future]
    
    underlying_price = st.session_state.underlying_price or cfg["default_price"]

    # PCR
    total_call_oi = strike_df["call_oi"].sum() if strike_df is not None and not strike_df.empty else 0
    total_put_oi = strike_df["put_oi"].sum() if strike_df is not None and not strike_df.empty else 0
    total_call_vol = strike_df["call_volume"].sum() if strike_df is not None and not strike_df.empty else 0
    total_put_vol = strike_df["put_volume"].sum() if strike_df is not None and not strike_df.empty else 0
    pcr_oi = total_put_oi / total_call_oi if total_call_oi > 0 else 0
    pcr_vol = total_put_vol / total_call_vol if total_call_vol > 0 else 0

    def pcr_label(pcr):
        if pcr == 0: return "N/A"
        if pcr < 0.7: return "🟢 Bullish"
        if pcr < 1.0: return "🟡 Neutral-Bull"
        if pcr < 1.3: return "🟠 Neutral-Bear"
        return "🔴 Bearish"

    price_fmt = f"{underlying_price:,.4f}" if cfg["increment"] < 1 else f"{underlying_price:,.2f}"
    cat_clr = "#00aaff" if cfg["category"] == "equity" else ("#ff6600" if cfg["category"] == "energy" else "#ffcc00")

    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
        <span style="font-size:1.4rem;">{cfg["emoji"]}</span>
        <span style="font-size:1.2rem;font-weight:700;font-family:'JetBrains Mono',monospace;color:{cat_clr};">
            {st.session_state.selected_future}
        </span>
        <span style="color:#8899aa;font-size:0.9rem;">{cfg["label"]} · Exp: {st.session_state.expiration}</span>
    </div>
    """, unsafe_allow_html=True)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        st.metric("Preço", price_fmt)
    with c2:
        st.metric("Opções c/ Dados", f"{metrics['num_options']:,}")
    with c3:
        net_gex_m = metrics["net_gex"] / 1_000_000
        st.metric("Net GEX", f"${net_gex_m:,.2f}M")
    with c4:
        if metrics.get("zero_gamma"):
            st.metric("Zero Gamma", f"{metrics['zero_gamma']:,.2f}")
        else:
            st.metric("Zero Gamma", "N/A")
    with c5:
        st.metric("PCR (OI)", f"{pcr_oi:.2f}" if pcr_oi > 0 else "N/A",
                  delta=pcr_label(pcr_oi), delta_color="off")
    with c6:
        st.metric("PCR (Vol)", f"{pcr_vol:.2f}" if pcr_vol > 0 else "N/A",
                  delta=pcr_label(pcr_vol), delta_color="off")

    st.divider()

    # GEX Chart
    st.header("🎯 Gamma Exposure por Strike")
    col_chart, col_stats = st.columns([3, 1])

    with col_chart:
        df = calc.get_gex_by_strike()
        if df.empty:
            st.warning("Sem dados de GEX. O dxFeed pode não ter dados de futuros para o seu plano de dados.")
            st.info("""
            **Possíveis causas:**
            - A licença dxFeed via `/api-quote-tokens` pode não incluir Greeks de futuros
            - Tente com opções de índices (SPX, NDX) no app original para confirmar que a conexão funciona
            - O acesso a futuros no dxFeed pode requerer um plano de dados diferente
            """)
        else:
            chart_type = st.radio("Tipo", ["Calls vs Puts", "Net GEX"], horizontal=True)
            fig = go.Figure()
            if chart_type == "Calls vs Puts":
                fig.add_trace(go.Bar(x=df["strike"], y=df["call_gex"], name="Call GEX", marker_color="#00cc66"))
                fig.add_trace(go.Bar(x=df["strike"], y=-df["put_gex"], name="Put GEX", marker_color="#ff4444"))
                bmode = "relative"
            else:
                colors = ["#00cc66" if x >= 0 else "#ff4444" for x in df["net_gex"]]
                fig.add_trace(go.Bar(x=df["strike"], y=df["net_gex"], name="Net GEX", marker_color=colors))
                bmode = "group"
                fig.add_hline(y=0, line_dash="dot", line_color="#555")

            fig.add_vline(x=underlying_price, line_dash="dash", line_color="orange",
                          line_width=2, annotation_text=price_fmt)
            if metrics.get("zero_gamma"):
                fig.add_vline(x=metrics["zero_gamma"], line_dash="dot", line_color="#aa44ff",
                              line_width=2, annotation_text=f"ZG:{metrics['zero_gamma']:,.1f}")
            fig.update_layout(
                title=f"{st.session_state.selected_future} GEX · {st.session_state.expiration}",
                xaxis_title="Strike", yaxis_title="GEX ($)",
                barmode=bmode, template="plotly_dark", height=500,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(13,17,23,0.8)",
                font=dict(family="JetBrains Mono, monospace", size=11)
            )
            st.plotly_chart(fig, use_container_width=True)

    with col_stats:
        st.subheader("📊 Resumo")
        st.metric("Call GEX", f"${metrics['total_call_gex']/1e6:,.2f}M")
        st.metric("Put GEX", f"${metrics['total_put_gex']/1e6:,.2f}M")
        st.metric("Net GEX", f"${metrics['net_gex']/1e6:,.2f}M")
        if metrics["max_gex_strike"]:
            st.divider()
            st.metric("Max GEX Strike", f"{metrics['max_gex_strike']:,.2f}")
        if metrics.get("zero_gamma"):
            st.divider()
            st.metric("Zero Gamma", f"{metrics['zero_gamma']:,.2f}")

    # IV Skew
    if strike_df is not None and not strike_df.empty:
        if strike_df["call_iv"].notna().any() or strike_df["put_iv"].notna().any():
            st.divider()
            st.header("📈 IV Skew")
            fig_iv = go.Figure()
            civ = strike_df[strike_df["call_iv"].notna()]
            piv = strike_df[strike_df["put_iv"].notna()]
            if not civ.empty:
                fig_iv.add_trace(go.Scatter(x=civ["strike"], y=civ["call_iv"]*100,
                    mode="lines+markers", name="Call IV", line=dict(color="#00cc66", width=2)))
            if not piv.empty:
                fig_iv.add_trace(go.Scatter(x=piv["strike"], y=piv["put_iv"]*100,
                    mode="lines+markers", name="Put IV", line=dict(color="#ff4444", width=2)))
            fig_iv.add_vline(x=underlying_price, line_dash="dash", line_color="orange",
                             annotation_text=price_fmt)
            fig_iv.update_layout(
                title=f"IV Skew — {st.session_state.selected_future} · {st.session_state.expiration}",
                xaxis_title="Strike", yaxis_title="IV (%)",
                template="plotly_dark", height=400,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(13,17,23,0.8)",
            )
            st.plotly_chart(fig_iv, use_container_width=True)

        # OI & Volume
        st.divider()
        st.header("📊 Volume & Open Interest")
        col3, col4 = st.columns(2)
        with col3:
            fig_oi = go.Figure()
            fig_oi.add_trace(go.Bar(x=strike_df["strike"], y=strike_df["call_oi"],
                name="Call OI", marker_color="#00cc66"))
            fig_oi.add_trace(go.Bar(x=strike_df["strike"], y=-strike_df["put_oi"],
                name="Put OI", marker_color="#ff4444"))
            fig_oi.add_vline(x=underlying_price, line_dash="dash", line_color="orange")
            fig_oi.update_layout(title="Open Interest", xaxis_title="Strike",
                barmode="relative", template="plotly_dark", height=380,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(13,17,23,0.8)")
            st.plotly_chart(fig_oi, use_container_width=True)
        with col4:
            vol_view = st.radio("Volume", ["Calls vs Puts", "Total"], horizontal=True,
                                index=0, key="vol_radio")
            fig_vol = go.Figure()
            if vol_view == "Calls vs Puts":
                fig_vol.add_trace(go.Bar(x=strike_df["strike"], y=strike_df["call_volume"],
                    name="Call Vol", marker_color="#99ffcc"))
                fig_vol.add_trace(go.Bar(x=strike_df["strike"], y=-strike_df["put_volume"],
                    name="Put Vol", marker_color="#ff9999"))
                bm = "relative"
            else:
                fig_vol.add_trace(go.Bar(x=strike_df["strike"],
                    y=strike_df["call_volume"]+strike_df["put_volume"],
                    name="Total Vol", marker_color="#aa88ff"))
                bm = "group"
            fig_vol.add_vline(x=underlying_price, line_dash="dash", line_color="orange")
            fig_vol.update_layout(title="Volume", xaxis_title="Strike",
                barmode=bm, template="plotly_dark", height=380,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(13,17,23,0.8)")
            st.plotly_chart(fig_vol, use_container_width=True)

        # Top strikes table
        st.subheader("🔝 Top Strikes")
        tab1, tab2 = st.tabs(["Por OI", "Por Volume"])
        with tab1:
            top = strike_df.nlargest(10, "total_oi")[["strike","call_oi","put_oi","total_oi"]].copy()
            top["strike"] = top["strike"].apply(lambda x: f"{x:,.2f}")
            top.columns = ["Strike","Call OI","Put OI","Total OI"]
            st.dataframe(top, hide_index=True, use_container_width=True)
        with tab2:
            top2 = strike_df.nlargest(10, "total_volume")[["strike","call_volume","put_volume","total_volume"]].copy()
            top2["strike"] = top2["strike"].apply(lambda x: f"{x:,.2f}")
            top2.columns = ["Strike","Call Vol","Put Vol","Total Vol"]
            st.dataframe(top2, hide_index=True, use_container_width=True)

    if st.session_state.auto_refresh:
        time.sleep(1)
        st.rerun()


if __name__ == "__main__":
    main()
