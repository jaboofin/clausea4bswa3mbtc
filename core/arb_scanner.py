"""
╔══════════════════════════════════════════════════════════════════╗
║  ARB SCANNER — Independent Fast-Polling Arbitrage Engine          ║
║                                                                    ║
║  Runs its OWN async loop (every 5-10s), separate from the         ║
║  directional 15-min trading cycle.                                 ║
║                                                                    ║
║  Discovers ALL live BTC up/down markets via slug pattern:          ║
║    btc-updown-{5m,15m,30m,1h}-{timestamp}                         ║
║                                                                    ║
║  Paginates through Gamma API to ensure nothing is missed.          ║
║                                                                    ║
║  When YES + NO < threshold → buys both sides instantly.            ║
║  No predictions involved — pure pricing gap capture.               ║
║                                                                    ║
║  Activated via: python bot.py --arb  /  python bot.py --arb-only   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import re
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger("arb_scanner")

# Slug pattern: btc-updown-{timeframe}-{unix_timestamp}
SLUG_PATTERN = re.compile(r'^btc-updown-(\d+m|\d+h)-(\d+)$')

TIMEFRAME_LABELS = {
    "5m": "5-Min", "15m": "15-Min", "30m": "30-Min", "1h": "1-Hour",
}


# ── Data Models ──────────────────────────────────────────────────

@dataclass
class ArbMarket:
    """A discovered BTC up/down market with both YES/NO sides."""
    condition_id: str
    question: str
    slug: str
    token_id_yes: str
    token_id_no: str
    price_yes: float
    price_no: float
    liquidity: float
    end_date: str
    timeframe: str          # "5m", "15m", "30m", "1h"
    volume: float = 0.0
    last_refreshed: float = 0.0

    @property
    def combined(self) -> float:
        return self.price_yes + self.price_no

    @property
    def edge_pct(self) -> float:
        return (1.0 - self.combined) * 100 if self.combined < 1.0 else 0.0

    @property
    def is_arb(self) -> bool:
        return self.combined > 0 and self.combined < 1.0

    @property
    def end_ts(self) -> float:
        """Parse end_date ISO string to unix timestamp."""
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(self.end_date.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0

    @property
    def time_remaining_secs(self) -> float:
        return max(0, self.end_ts - time.time())


@dataclass
class ArbExecution:
    """Record of an executed arb trade."""
    timestamp: float
    condition_id: str
    question: str
    timeframe: str
    price_yes: float
    price_no: float
    combined: float
    edge_pct: float
    size_per_side: float
    guaranteed_profit: float
    order_id_yes: Optional[str] = None
    order_id_no: Optional[str] = None
    status: str = "pending"       # pending, filled, partial, failed, dry_run


@dataclass
class ArbScannerConfig:
    """Configuration for the arb scanner."""
    poll_interval_secs: float = 8.0     # How often to scan (seconds)
    arb_threshold: float = 0.98         # Buy both if YES+NO < this
    min_edge_pct: float = 1.0           # Skip tiny edges below 1%
    size_per_side_usd: float = 10.0     # USD to buy each side
    max_daily_arb_trades: int = 50      # Daily limit on arb trade pairs
    max_daily_arb_budget: float = 200.0 # Max USD committed to arb per day
    min_liquidity_usd: float = 0.0      # Skip illiquid markets (0 = allow all)
    cooldown_per_market_secs: float = 120.0  # Don't re-arb same market within 2min
    scan_timeframes: list = field(default_factory=lambda: ["5m", "15m", "30m", "1h"])


class ArbScanner:
    """
    Independent arbitrage scanner.

    Runs a fast async loop separate from the directional trading cycle.
    Discovers ALL active BTC up/down markets via slug pattern matching
    and pagination. Captures YES+NO mispricing instantly.

    Usage:
        scanner = ArbScanner(config, polymarket_client)
        asyncio.create_task(scanner.run())
        ...
        scanner.stop()
    """

    def __init__(self, config: ArbScannerConfig, polymarket_client=None):
        self.config = config
        self.polymarket = polymarket_client
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

        # State
        self._known_markets: dict[str, ArbMarket] = {}
        self._expired_markets: dict[str, ArbMarket] = {}
        self._executions: list[ArbExecution] = []
        self._cooldowns: dict[str, float] = {}
        self._daily_trades = 0
        self._daily_spent = 0.0
        self._daily_profit = 0.0
        self._day_start = 0.0
        self._scan_count = 0
        self._last_discovery = 0.0
        self._last_scan_time_ms = 0.0
        self._near_misses: list[dict] = []
        self._best_edge_seen: float = 0.0
        self._total_markets_discovered = 0

        # Discovery cache
        self._discovery_interval = 45.0
        self._gamma_url = "https://gamma-api.polymarket.com"

    # ── HTTP Session ─────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def _close_session(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Market Discovery (paginated, slug-based) ─────────────────

    async def _discover_markets(self) -> list[ArbMarket]:
        """
        Fetch ALL active BTC up/down markets from Gamma API.
        Uses pagination (200 per page) and slug regex matching.
        """
        now = time.time()
        if now - self._last_discovery < self._discovery_interval and self._known_markets:
            return list(self._known_markets.values())

        try:
            session = await self._get_session()
            all_btc_markets = []
            offset = 0
            page_size = 200
            max_pages = 5

            for _ in range(max_pages):
                url = f"{self._gamma_url}/markets"
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": page_size,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "true",
                }

                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"Gamma API returned {resp.status}")
                        break
                    data = await resp.json()

                if not data:
                    break

                for m in data:
                    slug = m.get("slug", "")
                    match = SLUG_PATTERN.match(slug)
                    if not match:
                        continue

                    timeframe = match.group(1)
                    if timeframe not in self.config.scan_timeframes:
                        continue

                    tokens = m.get("tokens", [])
                    if len(tokens) < 2:
                        continue

                    token_id_yes = tokens[0].get("token_id", "")
                    token_id_no = tokens[1].get("token_id", "")
                    liquidity = float(m.get("liquidityClob", m.get("liquidityNum", 0)))
                    volume = float(m.get("volumeNum", m.get("volume", 0)))

                    market = ArbMarket(
                        condition_id=m.get("conditionId", m.get("id", "")),
                        question=m.get("question", ""),
                        slug=slug,
                        token_id_yes=token_id_yes,
                        token_id_no=token_id_no,
                        price_yes=float(tokens[0].get("price", 0)),
                        price_no=float(tokens[1].get("price", 0)),
                        liquidity=liquidity,
                        end_date=m.get("endDate", ""),
                        timeframe=timeframe,
                        volume=volume,
                        last_refreshed=now,
                    )
                    all_btc_markets.append(market)

                if len(data) < page_size:
                    break
                offset += page_size

            # Expire old markets
            for cid, mkt in list(self._known_markets.items()):
                if mkt.time_remaining_secs <= 0:
                    self._expired_markets[cid] = mkt
                    del self._known_markets[cid]

            # Add/update
            for market in all_btc_markets:
                self._known_markets[market.condition_id] = market

            self._last_discovery = now
            self._total_markets_discovered = len(self._known_markets)

            by_tf = {}
            for mkt in self._known_markets.values():
                by_tf.setdefault(mkt.timeframe, 0)
                by_tf[mkt.timeframe] += 1
            summary = " · ".join(
                f"{TIMEFRAME_LABELS.get(k, k)}: {v}"
                for k, v in sorted(by_tf.items())
            )
            logger.info(
                f"🔍 Discovered {len(self._known_markets)} BTC markets "
                f"(pages: {offset // page_size + 1}) — {summary}"
            )

            return list(self._known_markets.values())

        except Exception as e:
            logger.error(f"Discovery error: {e}")
            return list(self._known_markets.values())

    # ── Price Refresh ────────────────────────────────────────────

    async def _refresh_prices(self, markets: list[ArbMarket]) -> list[ArbMarket]:
        """Refresh YES/NO prices for known markets."""
        if not markets:
            return markets

        now = time.time()
        stale_threshold = self.config.poll_interval_secs * 0.8

        try:
            session = await self._get_session()
            for mkt in markets:
                if now - mkt.last_refreshed < stale_threshold:
                    continue
                if mkt.time_remaining_secs <= 0:
                    continue
                try:
                    url = f"{self._gamma_url}/markets/{mkt.condition_id}"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            tokens = data.get("tokens", [])
                            if len(tokens) >= 2:
                                mkt.price_yes = float(tokens[0].get("price", 0))
                                mkt.price_no = float(tokens[1].get("price", 0))
                                mkt.liquidity = float(data.get("liquidityClob", data.get("liquidityNum", 0)))
                                mkt.volume = float(data.get("volumeNum", data.get("volume", 0)))
                                mkt.last_refreshed = now
                except Exception:
                    pass

            return list(self._known_markets.values())
        except Exception as e:
            logger.error(f"Price refresh error: {e}")
            return markets

    # ── Arb Detection ────────────────────────────────────────────

    def _find_opportunities(self, markets: list[ArbMarket]) -> list[ArbMarket]:
        """Find markets where YES + NO < threshold."""
        now = time.time()
        opps = []

        for m in markets:
            if m.combined == 0:
                continue
            if m.time_remaining_secs <= 0:
                continue

            # Track near-misses (within 2% of threshold)
            if m.combined < self.config.arb_threshold + 0.02 and m.combined >= self.config.arb_threshold:
                self._near_misses = [nm for nm in self._near_misses if now - nm.get("time", 0) < 300]
                self._near_misses.append({
                    "time": now,
                    "question": m.question[:60],
                    "timeframe": m.timeframe,
                    "combined": round(m.combined, 4),
                    "gap": round((1.0 - m.combined) * 100, 2),
                })

            if m.combined >= self.config.arb_threshold:
                continue
            if m.edge_pct < self.config.min_edge_pct:
                continue
            if m.edge_pct > self._best_edge_seen:
                self._best_edge_seen = m.edge_pct

            last_arb = self._cooldowns.get(m.condition_id, 0)
            if now - last_arb < self.config.cooldown_per_market_secs:
                continue
            if m.liquidity < self.config.min_liquidity_usd:
                continue

            opps.append(m)

        return opps

    # ── Execution ────────────────────────────────────────────────

    async def _execute_arb(self, market: ArbMarket) -> Optional[ArbExecution]:
        """Buy both YES and NO sides of a mispriced market."""
        now = time.time()

        if self._daily_trades >= self.config.max_daily_arb_trades:
            return None
        cost = self.config.size_per_side_usd * 2
        if self._daily_spent + cost > self.config.max_daily_arb_budget:
            return None

        profit = self.config.size_per_side_usd * (1.0 / market.combined - 1.0)

        execution = ArbExecution(
            timestamp=now, condition_id=market.condition_id,
            question=market.question, timeframe=market.timeframe,
            price_yes=market.price_yes, price_no=market.price_no,
            combined=market.combined, edge_pct=market.edge_pct,
            size_per_side=self.config.size_per_side_usd,
            guaranteed_profit=round(profit, 2),
        )

        tf_label = TIMEFRAME_LABELS.get(market.timeframe, market.timeframe)
        logger.info(
            f"💰 ARB [{tf_label}]: {market.question[:60]}... | "
            f"YES={market.price_yes:.3f} + NO={market.price_no:.3f} = {market.combined:.3f} | "
            f"edge={market.edge_pct:.1f}% | profit=${profit:.2f}"
        )

        if self.polymarket:
            try:
                from core.polymarket_client import BinaryMarket, MarketStatus
                bm = BinaryMarket(
                    condition_id=market.condition_id, question=market.question,
                    slug=market.slug, token_id_up=market.token_id_yes,
                    token_id_down=market.token_id_no, price_up=market.price_yes,
                    price_down=market.price_no, volume=market.volume,
                    liquidity=market.liquidity, created_at="",
                    end_date=market.end_date, status=MarketStatus.ACTIVE,
                )
                yes_trade = await self.polymarket.place_order(
                    market=bm, direction="up",
                    size_usd=self.config.size_per_side_usd,
                    oracle_price=0.0, confidence=1.0,
                )
                if yes_trade:
                    execution.order_id_yes = yes_trade.order_id
                no_trade = await self.polymarket.place_order(
                    market=bm, direction="down",
                    size_usd=self.config.size_per_side_usd,
                    oracle_price=0.0, confidence=1.0,
                )
                if no_trade:
                    execution.order_id_no = no_trade.order_id
                if yes_trade and no_trade:
                    execution.status = "filled"
                elif yes_trade or no_trade:
                    execution.status = "partial"
                else:
                    execution.status = "failed"
            except Exception as e:
                execution.status = "failed"
                logger.error(f"Arb execution error: {e}")
        else:
            execution.status = "dry_run"

        self._executions.append(execution)
        self._cooldowns[market.condition_id] = now
        self._daily_trades += 1
        self._daily_spent += cost
        if execution.status in ("filled", "dry_run"):
            self._daily_profit += profit

        return execution

    # ── Daily Reset ──────────────────────────────────────────────

    def _check_daily_reset(self):
        now = time.time()
        if now - self._day_start > 86400:
            self._daily_trades = 0
            self._daily_spent = 0.0
            self._daily_profit = 0.0
            self._day_start = now
            self._best_edge_seen = 0.0
            self._near_misses.clear()

    # ── Main Loop ────────────────────────────────────────────────

    async def run(self):
        self._running = True
        self._day_start = time.time()

        logger.info(
            f"🚀 Arb scanner started — "
            f"polling every {self.config.poll_interval_secs}s | "
            f"timeframes: {', '.join(self.config.scan_timeframes)} | "
            f"threshold: {self.config.arb_threshold} | "
            f"budget: ${self.config.max_daily_arb_budget}/day"
        )

        while self._running:
            try:
                self._check_daily_reset()
                self._scan_count += 1
                scan_start = time.time()

                if time.time() - self._last_discovery > self._discovery_interval:
                    markets = await self._discover_markets()
                else:
                    markets = await self._refresh_prices(list(self._known_markets.values()))

                # Prune expired
                now = time.time()
                for cid in [c for c, m in self._known_markets.items() if m.time_remaining_secs <= 0]:
                    self._expired_markets[cid] = self._known_markets.pop(cid)

                opps = self._find_opportunities(list(self._known_markets.values()))
                if opps:
                    opps.sort(key=lambda m: m.edge_pct, reverse=True)
                    for opp in opps:
                        await self._execute_arb(opp)
                        if self._daily_trades >= self.config.max_daily_arb_trades:
                            break
                        if self._daily_spent >= self.config.max_daily_arb_budget:
                            break

                self._last_scan_time_ms = (time.time() - scan_start) * 1000

                if self._scan_count % 30 == 0:
                    by_tf = self._count_by_timeframe()
                    tf_str = ", ".join(f"{TIMEFRAME_LABELS.get(k,k)}:{v}" for k, v in sorted(by_tf.items()))
                    logger.info(
                        f"📡 Scan #{self._scan_count} | "
                        f"{len(self._known_markets)} live ({tf_str}) | "
                        f"today: {self._daily_trades} arbs, "
                        f"${self._daily_profit:.2f} profit | "
                        f"scan: {self._last_scan_time_ms:.0f}ms"
                    )

            except Exception as e:
                logger.error(f"Arb scan error: {e}", exc_info=True)

            await asyncio.sleep(self.config.poll_interval_secs)

        await self._close_session()
        logger.info("Arb scanner stopped")

    def stop(self):
        self._running = False

    # ── Stats / Dashboard ────────────────────────────────────────

    def get_stats(self) -> dict:
        """Rich stats for dashboard display."""
        by_tf = self._count_by_timeframe()

        market_list = []
        for m in sorted(self._known_markets.values(), key=lambda x: x.end_ts):
            market_list.append({
                "question": m.question[:70],
                "timeframe": m.timeframe,
                "tf_label": TIMEFRAME_LABELS.get(m.timeframe, m.timeframe),
                "price_yes": m.price_yes,
                "price_no": m.price_no,
                "combined": round(m.combined, 4),
                "edge_pct": round(m.edge_pct, 2),
                "liquidity": m.liquidity,
                "volume": m.volume,
                "time_remaining": m.time_remaining_secs,
                "end_date": m.end_date,
                "is_arb": m.combined > 0 and m.combined < self.config.arb_threshold,
            })

        return {
            "running": self._running,
            "scan_count": self._scan_count,
            "scan_time_ms": round(self._last_scan_time_ms, 1),
            "poll_interval": self.config.poll_interval_secs,
            "markets_live": len(self._known_markets),
            "markets_expired": len(self._expired_markets),
            "markets_by_timeframe": by_tf,
            "market_list": market_list[-50:],
            "threshold": self.config.arb_threshold,
            "size_per_side": self.config.size_per_side_usd,
            "timeframes": self.config.scan_timeframes,
            "daily_trades": self._daily_trades,
            "daily_profit": round(self._daily_profit, 2),
            "daily_spent": round(self._daily_spent, 2),
            "daily_budget": self.config.max_daily_arb_budget,
            "daily_budget_remaining": round(self.config.max_daily_arb_budget - self._daily_spent, 2),
            "daily_max_trades": self.config.max_daily_arb_trades,
            "best_edge_pct": round(self._best_edge_seen, 2),
            "near_misses": self._near_misses[-5:],
            "total_executions": len(self._executions),
            "recent_arbs": [
                {
                    "time": e.timestamp, "timeframe": e.timeframe,
                    "tf_label": TIMEFRAME_LABELS.get(e.timeframe, e.timeframe),
                    "edge_pct": round(e.edge_pct, 2), "profit": e.guaranteed_profit,
                    "status": e.status, "yes": e.price_yes, "no": e.price_no,
                    "combined": round(e.combined, 4), "question": e.question[:60],
                }
                for e in self._executions[-10:]
            ],
        }

    def _count_by_timeframe(self) -> dict:
        counts = {}
        for m in self._known_markets.values():
            counts.setdefault(m.timeframe, 0)
            counts[m.timeframe] += 1
        return counts

    def get_executions(self) -> list[ArbExecution]:
        return self._executions.copy()
