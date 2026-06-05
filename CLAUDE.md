# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**QTS — Quantum Trading System** is a cryptocurrency futures trading platform for Bybit perpetuals (hedge mode, Unified account). Two independent execution paths exist in parallel:

1. **QTS GUI system** — full signal pipeline + GTK4/TUI interface + AI strategy agent
2. **fast_auto_trader** — standalone WebSocket scalper (`scripts/fast_auto_trader.py`), no dependency on QTS core

## Setup & Running

```bash
./setup.sh                         # creates venv, installs deps, copies .env.example
source .venv/bin/activate

# QTS GUI system
python main.py                     # GTK4 desktop (requires GNOME/libadwaita)
python main_terminal.py            # Terminal TUI (works anywhere)
python mcp_server.py               # MCP server — exposes trading tools to Claude

# Fast scalper (independent, runs in background)
nohup python -u scripts/fast_auto_trader.py >> /tmp/fast_trader.log 2>&1 &
tail -f /tmp/fast_trader.log
python scripts/fast_auto_trader.py --dry-run          # no real orders
python scripts/fast_auto_trader.py --symbols XRP,ADA  # override symbols

# GTK position monitor (reads live from Bybit REST, independent of trader)
python interface/trade_monitor.py

# Diagnostics
python -m tools.analyze_trade      # live account + market context
python -m tools.verify_observability
```

No automated tests. Use `BYBIT_TESTNET=true` or `PAPER_TRADING=true` in `.env` for safe development.

## Two Independent Architectures

### 1. QTS GUI System (`main.py` / `mcp_server.py`)

```
Bybit WebSockets (public + private)
    ↓
streams/market.py   → orderbook, trades, CVD, liquidations, OI (MarketState)
streams/account.py  → positions, executions, balance
streams/klines.py   → REST kline poller (15m/1h), resampled every 90s
    ↓
core/absorption.py  → AbsorptionDetector: CVD divergence score 0-100
core/regime.py      → RegimeClassifier: RANGING | TRENDING_UP/DOWN | VOLATILE | ACCUMULATION
core/trend.py       → TrendAnalyzer: multi-TF Fibonacci-weighted score
core/liquidity.py   → LiquidityAnalyzer: HVN/LVN/EQ_H/EQ_L/ROUND levels
                    → OpportunityScorer: composite ≥70 to propose
    ↓
core/strategy.py    → rule-based OrderRequest
core/ai_strategy.py → LLM-based OrderRequest (OpenAI/Ollama/compatible)
    ↓
core/controller.py  → TradeController: lifecycle, AutoMode enforcement
core/executor.py    → Bybit REST v5
```

**AutoMode:** `MANUAL | SUGGEST | AUTO_ENTRY | FULL_AUTO`  
**TradeState:** `PENDING → SUBMITTED → OPEN → [BREAKEVEN] → [TRAILING] → CLOSED/FAILED`

### 2. Fast Auto-Trader (`scripts/fast_auto_trader.py`)

Self-contained — only imports `core/config.py` for API keys, everything else is internal.

- Subscribes to `kline.1` and `kline.3` WebSocket streams for 9 pairs + BTC reference
- Warm-up: fetches 1m/3m/15m/1h history via REST on startup
- **PairState** dataclass per symbol: EMA9/21 on 1m+3m+15m+1h, streak counter, live price/volume
- **Signal logic** (`check_signal()`): two paths:
  - *EMA path*: both 1m and 3m EMA9>EMA21 aligned + streak ≥ MIN_STREAK + m3 momentum + rvol + BTC divergence
  - *Momentum override*: 3 consecutive closes >0.15% in same direction with rvol ≥ 1.5 — bypasses EMA lag during sharp moves
- **HTF filter** (`htf_allows()`): blocks entries against 1h or 15m trend
- **Exit management** (monitor loop every 2s):
  - Emergency close: price moves ≥0.15% adverse in one tick
  - Auto-close: price within 0.05% of TP
  - BE: SL → entry±0.1% when PnL ≥ 40% of risk; cancels fixed TP, trail takes over
  - Trail: 0.1% trailing SL tick-by-tick post-BE
  - Flip: candle-based reversal signal → close + open opposite
- **Sizing**: `risk = equity × 2%`, `qty = risk / sl_distance`; capped at `MIN_NOTIONAL × 1.5`
- **Structural SL/TP**: nearest swing high/low from last 60 1m closes + round numbers; falls back to ATR×1.5/ATR×2

**Current scalping parameters:**
```python
SL_PCT=0.002, TP_PCT=0.004, LEVERAGE=50, MIN_STREAK=3, MAX_STREAK=7
MIN_M3_PCT=0.18, MIN_RVOL=0.8, MIN_DIVERGE=0.15, BE_TRIGGER=0.4
COOLDOWN_S=15, MAX_POSITIONS=2
```

**Blacklisted pairs** (0% win rate in live history): LINK, AVAX, HBAR, DOT, FIL, VET.

## Databases

### DuckDB — `storage/trading.duckdb`
Used exclusively by the **QTS GUI system**. Initialized by `core/db.initialize_db()`. Tables: `trades`, `tickers`, `trading_sessions`, `system_logs`. All structured events go through `core/logger.py` with trace IDs. **Do not open concurrently** — single-writer only.

### PostgreSQL — `trading` database (local Unix socket)
Used exclusively by **fast_auto_trader**. DSN: `postgresql://dev@/trading?host=/var/run/postgresql`

| Table | Content |
|-------|---------|
| `qts_trades` | One row per trade: entry/exit, SL/TP labels, R:R, PnL, close reason, signal params |
| `qts_ticks` | Every 2s monitor tick per open position: mark price, PnL, events (BE/TRAIL/FLIP/EMERGENCY) |
| `qts_signals` | Every signal detected: executed or rejected with reason |
| `qts_equity` | Balance snapshot every 30s |

## Key Configuration (`core/config.py`)

65+ Pydantic settings, auto-saved back to `.env` on change. Critical ones:
- `BYBIT_API_KEY/SECRET`, `BYBIT_TESTNET`
- `PAPER_TRADING=true` → fake $10k, no real orders
- `SPEED_LEVEL`: `nano | scalp | fast | standard` (ATR multipliers, timeframes for QTS system)
- `AI_PROVIDER`, `AI_MODEL` (OpenAI/Ollama/compatible)
- `TRADING_PG_DSN` → PostgreSQL connection for fast_auto_trader

## MCP Server (`mcp_server.py`)

Exposes trading as MCP tools for Claude: `get_signals`, `get_account`, `get_positions`, `get_symbol_data`, `place_order`, `close_position`, `modify_sl_tp`, `get_session_config`. Runs an asyncio loop in a background thread; MCP tools are synchronous and submit coroutines to that loop via `Future`.

## Bybit-Specific Constraints

- **Hedge mode**: `positionIdx=1` for LONG, `positionIdx=2` for SHORT — always required on order/stop endpoints
- **Unified account**: available margin = `usdt_equity - usdt_initialMargin` (not `totalAvailableBalance`)
- **Min notional**: $5 USDT for linear perpetuals
- **REST auth**: HMAC-SHA256 of `timestamp + apiKey + recvWindow + queryString`; `recvWindow=10000` to avoid timestamp drift

## Live Trading Operations

```bash
# Check if fast_auto_trader is running
pgrep -af fast_auto_trader

# Stop it cleanly
pkill -f fast_auto_trader.py

# Query trade history
psql "postgresql://dev@/trading?host=/var/run/postgresql" \
  -c "SELECT symbol, side, pnl, close_reason, duration_s FROM qts_trades ORDER BY ts DESC LIMIT 20;"

# Query Bybit closed PnL directly
# Use rest_get('/v5/position/closed-pnl', {'category':'linear','limit':'50'}) in Python
```

## interface/trade_monitor.py

Standalone GTK4 monitor that polls Bybit REST every 3s. Does **not** connect to DuckDB or PostgreSQL — reads directly from Bybit. Shows balance, open positions with mark price/PnL, and a live tick table with event detection (SL moves, near-TP warnings).
