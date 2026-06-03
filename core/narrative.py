"""
core/narrative.py — Tejido narrativo de símbolos.

Resuelve "qué representa cada par": nombre, sector, narrativas activas,
ecosistema, peers y descripción corta. Permite al usuario ver al instante
si un símbolo es L1, DeFi, AI, meme, parte de ecosistema Solana, etc.

Fuente: CoinGecko free API (/coins/{id}, /search).
Cache local: DuckDB tabla symbol_context con TTL 24h.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import aiohttp

from core.db import get_connection

log = logging.getLogger("qts.narrative")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
CACHE_TTL_S = 24 * 3600

# Mapping curado símbolo Bybit → coin_id CoinGecko (top ~50 por relevancia).
# Para los pares no listados se cae a /search dinámico y se cachea.
SYMBOL_TO_COIN_ID: dict[str, str] = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "BNBUSDT": "binancecoin",
    "XRPUSDT": "ripple",
    "ADAUSDT": "cardano",
    "DOGEUSDT": "dogecoin",
    "AVAXUSDT": "avalanche-2",
    "DOTUSDT": "polkadot",
    "MATICUSDT": "matic-network",
    "TRXUSDT": "tron",
    "LINKUSDT": "chainlink",
    "TONUSDT": "the-open-network",
    "SHIBUSDT": "shiba-inu",
    "LTCUSDT": "litecoin",
    "BCHUSDT": "bitcoin-cash",
    "UNIUSDT": "uniswap",
    "ATOMUSDT": "cosmos",
    "ICPUSDT": "internet-computer",
    "NEARUSDT": "near",
    "APTUSDT": "aptos",
    "SUIUSDT": "sui",
    "FILUSDT": "filecoin",
    "ARBUSDT": "arbitrum",
    "OPUSDT": "optimism",
    "INJUSDT": "injective-protocol",
    "TIAUSDT": "celestia",
    "SEIUSDT": "sei-network",
    "STXUSDT": "blockstack",
    "RUNEUSDT": "thorchain",
    "AAVEUSDT": "aave",
    "MKRUSDT": "maker",
    "LDOUSDT": "lido-dao",
    "RPLUSDT": "rocket-pool",
    "ENAUSDT": "ethena",
    "EIGENUSDT": "eigenlayer",
    "JTOUSDT": "jito-governance-token",
    "JUPUSDT": "jupiter-exchange-solana",
    "RAYUSDT": "raydium",
    "PYTHUSDT": "pyth-network",
    "BONKUSDT": "bonk",
    "WIFUSDT": "dogwifcoin",
    "PEPEUSDT": "pepe",
    "FLOKIUSDT": "floki",
    "TAOUSDT": "bittensor",
    "FETUSDT": "fetch-ai",
    "RNDRUSDT": "render-token",
    "AGIXUSDT": "singularitynet",
    "AKTUSDT": "akash-network",
    "ARUSDT": "arweave",
    "GRTUSDT": "the-graph",
    "ENSUSDT": "ethereum-name-service",
    "ORDIUSDT": "ordinals",
    "SATSUSDT": "sats-ordinals",
    "WLDUSDT": "worldcoin-wld",
    "STRKUSDT": "starknet",
    "MANTAUSDT": "manta-network",
    "JASMYUSDT": "jasmycoin",
    "GALAUSDT": "gala",
    "SANDUSDT": "the-sandbox",
    "AXSUSDT": "axie-infinity",
    "MANAUSDT": "decentraland",
}

# Prioridad de sectores: la primera category que matchee define el sector primario.
# Las claves se comparan en minúsculas con espacios → guiones.
SECTOR_PRIORITY: list[tuple[str, str]] = [
    ("layer-1", "Layer 1"),
    ("layer-1-(l1)", "Layer 1"),
    ("smart-contract-platform", "Layer 1"),
    ("layer-2-(l2)", "Layer 2"),
    ("layer-2", "Layer 2"),
    ("zk-rollup", "Layer 2"),
    ("optimistic-rollup", "Layer 2"),
    ("artificial-intelligence-(ai)", "AI"),
    ("artificial-intelligence", "AI"),
    ("ai-agents", "AI"),
    ("ai-meme", "AI Meme"),
    ("liquid-staking-tokens", "Liquid Staking"),
    ("liquid-staking", "Liquid Staking"),
    ("restaking", "Restaking"),
    ("decentralized-exchange-(dex)", "DEX"),
    ("decentralized-exchange", "DEX"),
    ("decentralized-finance-(defi)", "DeFi"),
    ("decentralized-finance-defi", "DeFi"),
    ("real-world-assets-(rwa)", "RWA"),
    ("real-world-assets-rwa", "RWA"),
    ("tokenized-treasury-bonds-(t-bonds)", "RWA"),
    ("depin", "DePIN"),
    ("depin-tokens", "DePIN"),
    ("oracle", "Oracle"),
    ("storage", "Storage"),
    ("privacy-coins", "Privacy"),
    ("gaming-(gamefi)", "GameFi"),
    ("gaming", "GameFi"),
    ("metaverse", "Metaverse"),
    ("meme-token", "Meme"),
    ("memes", "Meme"),
    ("bridge-governance-tokens", "Bridge"),
    ("interoperability", "Interop"),
    ("modular-blockchain", "Modular"),
    ("data-availability", "Data Avail."),
    ("perpetuals", "Perps"),
    ("yield-farming", "Yield"),
    ("yield-aggregator", "Yield"),
    ("exchange-based-tokens", "CEX Token"),
    ("stablecoins", "Stablecoin"),
    ("nft", "NFT"),
    ("infrastructure", "Infra"),
]

# Etiquetas narrativas (chips). Cumulativas: un símbolo puede tener varias.
# CoinGecko serializa categorías con paréntesis: incluimos ambas formas.
NARRATIVE_TAGS: list[tuple[str, str]] = [
    ("artificial-intelligence", "AI"),
    ("artificial-intelligence-(ai)", "AI"),
    ("ai-agents", "AI Agents"),
    ("real-world-assets-rwa", "RWA"),
    ("real-world-assets-(rwa)", "RWA"),
    ("depin", "DePIN"),
    ("depin-tokens", "DePIN"),
    ("liquid-staking-tokens", "LST"),
    ("liquid-staking", "LST"),
    ("restaking", "Restaking"),
    ("modular-blockchain", "Modular"),
    ("data-availability", "DA"),
    ("meme-token", "Meme"),
    ("memes", "Meme"),
    ("gaming", "GameFi"),
    ("gaming-(gamefi)", "GameFi"),
    ("metaverse", "Metaverse"),
    ("nft", "NFT"),
    ("bitcoin-ecosystem", "BTC Eco"),
    ("solana-ecosystem", "SOL Eco"),
    ("ethereum-ecosystem", "ETH Eco"),
    ("base-ecosystem", "Base Eco"),
    ("ton-ecosystem", "TON Eco"),
    ("perpetuals", "Perps"),
    ("decentralized-exchange", "DEX"),
    ("decentralized-exchange-(dex)", "DEX"),
    ("decentralized-finance-defi", "DeFi"),
    ("decentralized-finance-(defi)", "DeFi"),
    ("oracle", "Oracle"),
]

# Reglas de detección de ecosistema (primera coincidencia gana).
ECOSYSTEM_RULES: list[tuple[str, str]] = [
    ("solana-ecosystem", "Solana"),
    ("ethereum-ecosystem", "Ethereum"),
    ("bitcoin-ecosystem", "Bitcoin"),
    ("base-ecosystem", "Base"),
    ("arbitrum-ecosystem", "Arbitrum"),
    ("optimism-ecosystem", "Optimism"),
    ("avalanche-ecosystem", "Avalanche"),
    ("polygon-ecosystem", "Polygon"),
    ("bnb-chain-ecosystem", "BNB Chain"),
    ("binance-smart-chain", "BNB Chain"),
    ("ton-ecosystem", "TON"),
    ("sui-ecosystem", "Sui"),
    ("aptos-ecosystem", "Aptos"),
    ("cosmos-ecosystem", "Cosmos"),
    ("cardano-ecosystem", "Cardano"),
    ("near-protocol-ecosystem", "NEAR"),
]


@dataclass
class SymbolContext:
    symbol: str
    name: str = ""
    sector: str = "Other"
    narratives: list[str] = field(default_factory=list)
    ecosystem: str = "Native"
    sector_peers: list[str] = field(default_factory=list)
    ecosystem_children: list[str] = field(default_factory=list)
    description_short: str = ""
    market_cap_rank: int = 0
    fetched_at: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Helpers de normalización ────────────────────────────────────────────────


def _strip_quote(symbol: str) -> str:
    for suffix in ("USDT", "USDC", "USD"):
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _slugify(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "-")


def _classify(categories: list[str]) -> tuple[str, list[str], str]:
    """Devuelve (sector, narrativas, ecosistema) desde la lista de categories."""
    slugs = {_slugify(c) for c in categories if c}

    sector = "Other"
    for slug, label in SECTOR_PRIORITY:
        if slug in slugs:
            sector = label
            break

    narratives: list[str] = []
    seen: set[str] = set()
    for slug, label in NARRATIVE_TAGS:
        if slug in slugs and label not in seen:
            narratives.append(label)
            seen.add(label)
        if len(narratives) >= 5:
            break

    ecosystem = "Native"
    for slug, label in ECOSYSTEM_RULES:
        if slug in slugs:
            ecosystem = label
            break

    return sector, narratives, ecosystem


def _extract_description(coin_data: dict) -> str:
    desc = ((coin_data.get("description") or {}).get("en") or "").strip()
    if not desc:
        return ""
    # Quita marcadores tipo *bold* y enlaces markdown simples
    desc = desc.replace("\r", " ").replace("\n", " ")
    sentence = desc.split(". ")[0].strip()
    if len(sentence) > 200:
        sentence = sentence[:197].rstrip() + "…"
    return sentence


# ─── Persistencia ────────────────────────────────────────────────────────────


def init_tables() -> None:
    """Crea symbol_context y symbol_correlations. Idempotente."""
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS symbol_context (
                symbol            VARCHAR PRIMARY KEY,
                name              VARCHAR DEFAULT '',
                sector            VARCHAR DEFAULT 'Other',
                narratives_json   TEXT    DEFAULT '[]',
                ecosystem         VARCHAR DEFAULT 'Native',
                sector_peers_json TEXT    DEFAULT '[]',
                eco_children_json TEXT    DEFAULT '[]',
                description_short TEXT    DEFAULT '',
                market_cap_rank   INTEGER DEFAULT 0,
                fetched_at        BIGINT  DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS symbol_correlations (
                symbol_a   VARCHAR NOT NULL,
                symbol_b   VARCHAR NOT NULL,
                corr_30d   DOUBLE  DEFAULT 0,
                beta_30d   DOUBLE  DEFAULT 0,
                window_h   INTEGER DEFAULT 720,
                updated_at BIGINT  DEFAULT 0,
                PRIMARY KEY (symbol_a, symbol_b)
            )
        """)
        try:
            con.execute("CREATE INDEX IF NOT EXISTS idx_sc_a ON symbol_correlations (symbol_a)")
        except Exception:
            pass
    finally:
        con.close()


def load_context(symbol: str) -> Optional[SymbolContext]:
    """Lee contexto cacheado. None si no existe o expiró."""
    try:
        con = get_connection()
        row = con.execute("""
            SELECT name, sector, narratives_json, ecosystem,
                   sector_peers_json, eco_children_json,
                   description_short, market_cap_rank, fetched_at
            FROM symbol_context WHERE symbol = ?
        """, (symbol,)).fetchone()
        con.close()
        if not row:
            return None
        fetched_at = int(row[8] or 0)
        if (int(time.time()) - fetched_at) > CACHE_TTL_S:
            return None
        return SymbolContext(
            symbol=symbol,
            name=row[0] or "",
            sector=row[1] or "Other",
            narratives=json.loads(row[2] or "[]"),
            ecosystem=row[3] or "Native",
            sector_peers=json.loads(row[4] or "[]"),
            ecosystem_children=json.loads(row[5] or "[]"),
            description_short=row[6] or "",
            market_cap_rank=int(row[7] or 0),
            fetched_at=fetched_at,
        )
    except Exception as e:
        log.error("load_context(%s) falló: %s", symbol, e)
        return None


def save_context(ctx: SymbolContext) -> None:
    try:
        con = get_connection()
        con.execute("""
            INSERT OR REPLACE INTO symbol_context
                (symbol, name, sector, narratives_json, ecosystem,
                 sector_peers_json, eco_children_json,
                 description_short, market_cap_rank, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            ctx.symbol, ctx.name, ctx.sector,
            json.dumps(ctx.narratives),
            ctx.ecosystem,
            json.dumps(ctx.sector_peers),
            json.dumps(ctx.ecosystem_children),
            ctx.description_short,
            ctx.market_cap_rank,
            ctx.fetched_at or int(time.time()),
        ))
        con.close()
    except Exception as e:
        log.error("save_context(%s) falló: %s", ctx.symbol, e)


# ─── Cliente CoinGecko (throttled) ───────────────────────────────────────────

_session: Optional[aiohttp.ClientSession] = None
_rate_lock = asyncio.Semaphore(1)   # CG free es estricto; serializamos
_last_call_ts: float = 0.0
_CALL_SPACING_S: float = 2.5         # ~24 reqs/min (margen sobre el límite de 30)


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    return _session


async def _cg_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """GET a CoinGecko con throttling cooperativo."""
    global _last_call_ts
    async with _rate_lock:
        wait = _CALL_SPACING_S - (time.monotonic() - _last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_ts = time.monotonic()
        try:
            session = await _get_session()
            async with session.get(f"{COINGECKO_BASE}{path}", params=params) as resp:
                if resp.status == 429:
                    log.warning("CoinGecko %s → 429 rate limit", path)
                    await asyncio.sleep(15)
                    return None
                if resp.status != 200:
                    log.info("CoinGecko %s → HTTP %s", path, resp.status)
                    return None
                return await resp.json()
        except Exception as e:
            log.warning("CoinGecko %s falló: %s", path, e)
            return None


async def _resolve_coin_id(symbol: str) -> Optional[str]:
    """Devuelve coin_id de CoinGecko para un símbolo Bybit."""
    if symbol in SYMBOL_TO_COIN_ID:
        return SYMBOL_TO_COIN_ID[symbol]
    ticker = _strip_quote(symbol)
    if ticker in SYMBOL_TO_COIN_ID:
        return SYMBOL_TO_COIN_ID[ticker]
    data = await _cg_get("/search", {"query": ticker})
    if not data:
        return None
    coins = (data or {}).get("coins") or []
    # Match exacto por símbolo; ordena por market_cap_rank ascendente
    exact = [c for c in coins if str(c.get("symbol", "")).upper() == ticker]
    pool = exact or coins
    pool.sort(key=lambda c: c.get("market_cap_rank") or 1_000_000)
    return pool[0].get("id") if pool else None


async def fetch_context(symbol: str) -> Optional[SymbolContext]:
    """Descarga el contexto desde CoinGecko, lo cachea y lo devuelve."""
    coin_id = await _resolve_coin_id(symbol)
    if not coin_id:
        log.info("narrative: no se pudo resolver coin_id para %s", symbol)
        # Cacheamos un stub mínimo para no martillar /search en cada visita
        stub = SymbolContext(symbol=symbol, name=_strip_quote(symbol), fetched_at=int(time.time()))
        save_context(stub)
        return stub
    data = await _cg_get(
        f"/coins/{coin_id}",
        {
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false",
        },
    )
    if not data:
        return load_context(symbol)

    categories = [c for c in (data.get("categories") or []) if c]
    sector, narratives, ecosystem = _classify(categories)

    ctx = SymbolContext(
        symbol=symbol,
        name=data.get("name") or _strip_quote(symbol),
        sector=sector,
        narratives=narratives,
        ecosystem=ecosystem,
        sector_peers=[],
        ecosystem_children=[],
        description_short=_extract_description(data),
        market_cap_rank=int(data.get("market_cap_rank") or 0),
        fetched_at=int(time.time()),
    )
    save_context(ctx)
    return ctx


async def get_context(symbol: str, refresh: bool = False) -> Optional[SymbolContext]:
    """Devuelve contexto cacheado o lo descarga si caducó/no existe."""
    if not refresh:
        cached = load_context(symbol)
        if cached is not None:
            return cached
    return await fetch_context(symbol)


async def warmup_contexts(symbols: list[str]) -> int:
    """Pre-carga contextos respetando rate limit. Devuelve cuántos quedaron OK."""
    ok = 0
    for sym in symbols:
        ctx = await get_context(sym)
        if ctx and ctx.name:
            ok += 1
    return ok


# ─── Enriquecimiento local (peers / eco_children) ────────────────────────────


def enrich_peers_from_cache(top_per_group: int = 6) -> int:
    """
    Recorre la cache de symbol_context y rellena sector_peers/ecosystem_children
    cruzando símbolos entre sí. Es gratis (no toca CoinGecko).
    Devuelve cuántos símbolos quedaron enriquecidos.
    """
    try:
        con = get_connection()
        rows = con.execute("""
            SELECT symbol, name, sector, ecosystem, market_cap_rank
            FROM symbol_context
        """).fetchall()

        by_sector: dict[str, list[tuple[str, int]]] = {}
        by_eco:    dict[str, list[tuple[str, int]]] = {}
        for sym, _name, sector, eco, rank in rows:
            if sector and sector != "Other":
                by_sector.setdefault(sector, []).append((sym, int(rank or 1_000_000)))
            if eco and eco not in ("", "Native"):
                by_eco.setdefault(eco, []).append((sym, int(rank or 1_000_000)))

        for lst in by_sector.values():
            lst.sort(key=lambda x: x[1])
        for lst in by_eco.values():
            lst.sort(key=lambda x: x[1])

        updated = 0
        for sym, _name, sector, eco, _rank in rows:
            peers: list[str] = []
            if sector in by_sector:
                peers = [s for s, _ in by_sector[sector] if s != sym][:top_per_group]
            children: list[str] = []
            if eco in by_eco:
                children = [s for s, _ in by_eco[eco] if s != sym][:top_per_group]
            con.execute("""
                UPDATE symbol_context
                SET sector_peers_json = ?, eco_children_json = ?
                WHERE symbol = ?
            """, (json.dumps(peers), json.dumps(children), sym))
            updated += 1
        con.close()
        return updated
    except Exception as e:
        log.error("enrich_peers_from_cache falló: %s", e)
        return 0


# ─── Helpers de correlación (alimentados por job externo) ───────────────────


def save_correlations(rows: list[tuple[str, str, float, float, int]]) -> None:
    """Bulk-insert de filas (symbol_a, symbol_b, corr, beta, window_h)."""
    if not rows:
        return
    try:
        now = int(time.time())
        con = get_connection()
        con.executemany("""
            INSERT OR REPLACE INTO symbol_correlations
                (symbol_a, symbol_b, corr_30d, beta_30d, window_h, updated_at)
            VALUES (?,?,?,?,?,?)
        """, [(a, b, float(c), float(beta), int(w), now) for a, b, c, beta, w in rows])
        con.close()
    except Exception as e:
        log.error("save_correlations falló: %s", e)


def get_correlations_for(symbol: str, limit: int = 10, min_abs: float = 0.3) -> list[dict]:
    """Devuelve correlaciones de un símbolo ordenadas por |corr| desc."""
    try:
        con = get_connection()
        rows = con.execute("""
            SELECT symbol_b, corr_30d, beta_30d, updated_at
            FROM symbol_correlations
            WHERE symbol_a = ? AND ABS(corr_30d) >= ?
            ORDER BY ABS(corr_30d) DESC LIMIT ?
        """, (symbol, float(min_abs), int(limit))).fetchall()
        con.close()
        return [
            {
                "symbol": r[0],
                "corr":   round(float(r[1] or 0), 3),
                "beta":   round(float(r[2] or 0), 3),
                "updated_at": int(r[3] or 0),
            }
            for r in rows
        ]
    except Exception as e:
        log.error("get_correlations_for(%s) falló: %s", symbol, e)
        return []


async def close_session() -> None:
    """Cierra la sesión HTTP global. Llamar al shutdown del servidor."""
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None
