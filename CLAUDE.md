# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**QTS — Quantum Trading System** is a cryptocurrency futures trading platform for Bybit perpetuals. It combines real-time market intelligence (absorption, regime, trend, liquidity signals), AI-powered strategy generation (LLM-based), and automated trade execution with strict risk management.

## Setup & Running

```bash
# Initial setup (creates venv, installs deps, copies .env.example)
./setup.sh

# Activate venv before any work
source .venv/bin/activate

# GTK4 desktop GUI (requires GNOME/libadwaita)
python main.py

# Terminal TUI (works anywhere)
python main_terminal.py

# MCP server for Claude integration
python mcp_server.py

# Diagnostic: live account + market context analysis
python -m tools.analyze_trade

# Check observability pipeline
python -m tools.verify_observability
```

There are no automated tests. Use `BYBIT_TESTNET=true` or `PAPER_TRADING=true` in `.env` for safe development.

## Architecture

### Data Flow

```
Bybit WebSockets (public + private)
    ↓
MarketStream (orderbook, trades, CVD, liquidations, OI)
AccountStream (positions, executions, balance)
KlineStream (REST poll: 15m/1h, resampled every 90s)
    ↓
Signal Calculation (every ~30s scan interval)
  AbsorptionDetector  → score 0-100 (CVD divergence, flow efficiency)
  RegimeClassifier    → RANGING | TRENDING_UP/DOWN | VOLATILE | ACCUMULATION
  TrendAnalyzer       → multi-TF weighted score (Fibonacci: 1m×1 ... 6h×21)
  LiquidityAnalyzer   → S/R levels, OI velocity
  OpportunityScorer   → composite score; threshold ≥70 to propose
    ↓
Strategy Layer (StrategyEngine or AIStrategyAgent)
  Input:  top-N symbols by opp.score, balance, session goal
  Output: OrderRequest (entry, SL, TP, qty, reasoning)
    ↓
TradeController (orchestrates lifecycle, enforces modes)
    ↓
BybitExecutor (REST v5: place, adjust SL/TP, cancel)
```

### Automation Modes (AutoMode enum)

| Mode | Behavior |
|------|----------|
| `MANUAL` | User places orders manually |
| `SUGGEST` | System proposes, user confirms with 1 click |
| `AUTO_ENTRY` | Auto-enters, user adjusts stops |
| `FULL_AUTO` | Fully autonomous: entry → breakeven → trail → close |

### Trade Lifecycle (TradeState enum)

`PENDING → SUBMITTED → OPEN → [BREAKEVEN] → [TRAILING] → CLOSED/FAILED`

Breakeven moves SL to entry+fees at +1R; trailing activates at +2R.

### Risk Framework

- **RiskFortress** (`core/risk.py`): daily loss circuit breaker (default -2%), margin alerts at 60%/80%
- **SessionManager** (`core/session.py`): TSAA model — bounded sessions with PnL target, drawdown stop, time limit, API cost cap. States: `ACTIVE → HARVESTING | LIQUIDATING | API_EXHAUSTED → CLOSED`

### AI Strategy Agent (`core/ai_strategy.py`)

Filters top-N candidates (score ≥70), builds market snapshot per symbol, sends to LLM, parses JSON response `{action, symbol, side, entry, sl, tp, confidence, reasoning}`. Validates R:R ≥2.0 net of fees. Minimum 60s between calls; discards proposals older than 45s.

**Supported LLM providers:** OpenAI (default: gpt-4o), Ollama (local), or any OpenAI-compatible API (Groq, Mistral, etc.).

## Key Configuration (`core/config.py`)

65+ Pydantic settings loaded from `.env`. Auto-saves back to `.env` on any change. Key groups:
- `BYBIT_API_KEY/SECRET`, `BYBIT_TESTNET`
- `SYMBOLS` (comma-separated), `DEFAULT_SYMBOL`
- `SPEED_LEVEL`: `nano | scalp | fast | standard` (affects ATR multipliers, timeframes)
- `MAX_DAILY_LOSS_PCT`, `MAX_TRADES_PER_DAY`
- `AI_PROVIDER`, `AI_MODEL`, `AI_INTERVAL`
- `SESSION_*`: duration, target PnL, drawdown, API cost limit
- `PAPER_TRADING=true` for simulation with fake $10k

## Database (DuckDB at `storage/trading.duckdb`)

Initialized by `core/db.initialize_db()` at startup. Tables: `trades`, `tickers`, `trading_sessions`, `system_logs`. All structured events (with trace IDs) go through `core/logger.py` → `system_logs`.

## UI Architecture

**GTK4 GUI** (`interface/gtk_app.py`): AsyncBridge runs async I/O on a separate thread; GLib.timeout_add(100ms) drives UI refresh. Panels: OrderBook, Market Intelligence, Tape, Positions, Session, Risk, CVD chart (Cairo).

**Terminal TUI** (`interface/terminal.py`): Textual framework, same data sources, no GTK dependency.

## Module Map

| Path | Role |
|------|------|
| `core/controller.py` | TradeController: trade orchestration |
| `core/executor.py` | Bybit REST v5 client |
| `core/strategy.py` | Rule-based OrderRequest generation |
| `core/ai_strategy.py` | LLM-based OrderRequest generation |
| `core/order_model.py` | Data models: OrderRequest, TradeRecord, TradeState, AutoMode |
| `core/session.py` | SessionManager (TSAA) |
| `core/risk.py` | RiskFortress |
| `core/config.py` | Settings with auto-persist |
| `core/db.py` | DuckDB init + persistence |
| `core/logger.py` | StructuredLogger con trace IDs; instancias: `strategy_logger`, `executor_logger`, `controller_logger`, `risk_logger`, `system_logger` |
| `core/log_analyst.py` | LogAnalystAgent — consulta DB y usa LLM para analizar patrones de logs y trades |
| `core/absorption.py` | AbsorptionDetector (CVD-based) |
| `core/regime.py` | RegimeClassifier |
| `core/trend.py` | Multi-timeframe TrendAnalyzer |
| `core/liquidity.py` | S/R and order book profile |
| `core/technicals.py` | ATR, EMA, RSI from klines |
| `streams/market.py` | Public WebSocket: orderbook, trades, CVD, liquidations |
| `streams/account.py` | Private WebSocket: positions, executions, balance |
| `streams/klines.py` | REST kline poller (resampled) |
| `interface/gtk_app.py` | Main GTK4/Adwaita GUI |
| `interface/terminal.py` | Textual TUI |
