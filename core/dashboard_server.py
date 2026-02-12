"""
Dashboard Server — HTTP + WebSocket via aiohttp.

  http://localhost:8765      → Dashboard HTML page
  ws://localhost:8765/ws     → Live state broadcast
  http://localhost:8765/state → JSON snapshot

  python bot.py --bankroll 500 --dashboard
  → Open http://localhost:8765 in your browser
"""

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp
from aiohttp import web

logger = logging.getLogger("dashboard")


class DashboardServer:
    def __init__(self, host="0.0.0.0", port=8765):
        self.host = host
        self.port = port
        self.clients: set[web.WebSocketResponse] = set()
        self._state: dict = {}
        self._running = False
        self._runner: Optional[web.AppRunner] = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self._handle_page)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/state", self._handle_state)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self._running = True
        logger.info(f"Dashboard: http://localhost:{self.port}")

    async def _handle_page(self, request):
        return web.Response(text=_build_html(), content_type="text/html")

    async def _handle_state(self, request):
        return web.json_response(self._state)

    async def _handle_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)
        logger.info(f"Dashboard client connected ({len(self.clients)} total)")
        try:
            if self._state:
                await ws.send_json(self._state)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.PING:
                    await ws.pong(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            self.clients.discard(ws)
            logger.info(f"Dashboard client disconnected ({len(self.clients)} remaining)")
        return ws

    async def broadcast(self, state: dict):
        self._state = state
        dead = set()
        for ws in self.clients:
            try:
                await ws.send_json(state)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    async def stop(self):
        self._running = False
        for ws in list(self.clients):
            await ws.close()
        self.clients.clear()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Dashboard server stopped")

    @property
    def client_count(self):
        return len(self.clients)

    @property
    def is_running(self):
        return self._running


def build_dashboard_state(cycle, consensus, anchor, decision, risk_manager, polymarket_client, edge_config, config, arb_scanner=None):
    stats = polymarket_client.get_stats()
    risk_status = risk_manager.get_status()
    open_trades = polymarket_client.get_trade_records()

    signals = {}
    for s in (decision.signals if decision else []):
        signals[s.name] = {"direction": s.direction.value, "strength": round(s.strength, 3), "raw_value": round(s.raw_value, 4), "description": s.description}

    open_pos = []
    for t in open_trades:
        if t.outcome is None:
            open_pos.append({"id": t.trade_id, "direction": t.direction, "size_usd": t.size_usd, "entry_price": t.entry_price, "confidence": t.confidence, "timestamp": t.timestamp, "oracle_price": t.oracle_price_at_entry})

    closed_pos = []
    for t in open_trades:
        if t.outcome is not None:
            closed_pos.append({"id": t.trade_id, "direction": t.direction, "size_usd": t.size_usd, "entry_price": t.entry_price, "confidence": t.confidence, "pnl": t.pnl, "outcome": t.outcome, "timestamp": t.timestamp})

    arb_stats = arb_scanner.get_stats() if arb_scanner else None

    return {
        "type": "state", "timestamp": time.time(), "cycle": cycle,
        "oracle": {"price": consensus.price if consensus else 0, "chainlink": consensus.chainlink_price if consensus else None, "sources": consensus.sources if consensus else [], "spread_pct": consensus.spread_pct if consensus else 0},
        "anchor": {"open_price": anchor.open_price if anchor else None, "source": anchor.source if anchor else None, "drift_pct": decision.drift_pct if decision else None},
        "strategy": {"direction": decision.direction.value if decision else "hold", "confidence": decision.confidence if decision else 0, "should_trade": decision.should_trade if decision else False, "reason": decision.reason if decision else "", "drift_pct": decision.drift_pct if decision else None, "volatility_pct": decision.volatility_pct if decision else 0},
        "signals": signals,
        "stats": {"wins": stats.get("wins", 0), "losses": stats.get("losses", 0), "win_rate": stats.get("win_rate", 0), "total_pnl": stats.get("total_pnl", 0), "total_wagered": stats.get("total_wagered", 0), "total_trades": stats.get("total_trades", 0)},
        "risk": {"daily_trades": risk_status.get("daily_trades", 0), "max_daily_trades": config.risk.max_daily_trades, "daily_loss_pct": risk_status.get("daily_loss_pct", 0), "consecutive_losses": risk_status.get("consecutive_losses", 0), "cooldown_active": risk_status.get("cooldown_active", False)},
        "positions": {"open": open_pos, "closed": closed_pos[-50:]},
        "arb_scanner": arb_stats,
        "config": {"bankroll": config.bankroll, "arb_enabled": edge_config.enable_arb, "hedge_enabled": edge_config.enable_hedge},
    }


# ── HTML Builder ─────────────────────────────────────────────────

def _build_html():
    """Build the dashboard HTML. Separated for readability."""

    CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans',sans-serif;background:#f8fafc;color:#1e293b}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:2px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.mono{font-family:'DM Mono',monospace}
.card{background:#fff;border-radius:10px;border:1.5px solid #e2e8f0;padding:14px 16px}
.hero{background:linear-gradient(135deg,#16a34a 0%,#059669 40%,#0d9488 100%);padding:20px 28px 22px;color:#fff;position:relative;overflow:hidden}
.hero .c1{position:absolute;top:-40px;right:-40px;width:160px;height:160px;border-radius:50%;background:rgba(255,255,255,.06)}
.hero .c2{position:absolute;bottom:-30px;right:80px;width:100px;height:100px;border-radius:50%;background:rgba(255,255,255,.04)}
.sc{background:#fff;border-radius:10px;padding:16px 18px;position:relative;overflow:hidden}
.sc .ac{position:absolute;top:0;left:0;right:0;height:3px}
.sc .lb{font-size:11px;color:#94a3b8;font-weight:600;letter-spacing:.04em;text-transform:uppercase;margin-bottom:6px}
.sc .vl{font-size:26px;font-weight:800;line-height:1.1}
.sc .sb{font-size:11px;color:#94a3b8;margin-top:6px}
.sr{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #f1f5f9}
.si{width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0}
.su{background:#dcfce7;color:#16a34a}.sd{background:#fee2e2;color:#dc2626}
.g5{display:grid;grid-template-columns:1.2fr 1fr 1fr 1fr 1fr;gap:10px;margin-bottom:16px}
.gm{display:grid;grid-template-columns:1fr 300px;gap:14px}
.hs{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px}
.hs>div{background:rgba(255,255,255,.12);border-radius:8px;padding:10px 14px;text-align:center}
.hs .hl{font-size:10px;opacity:.7;margin-bottom:2px}.hs .hv{font-size:18px;font-weight:800}
.er{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #f1f5f9}
.dot{width:8px;height:8px;border-radius:50%}
.bg{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px}
.tab-btn{padding:6px 16px;border:none;background:none;border-radius:6px;cursor:pointer;font-family:inherit;font-size:12px;font-weight:500;color:#94a3b8;transition:all .15s}
.tab-btn.active{background:#f1f5f9;font-weight:700;color:#1e293b}
.tbl-hdr{display:grid;grid-template-columns:56px 50px 1fr 56px 64px 56px;font-size:10px;font-weight:600;color:#94a3b8;padding:0 0 6px;border-bottom:1.5px solid #e2e8f0;letter-spacing:.03em;text-transform:uppercase}
.tbl-row{display:grid;grid-template-columns:56px 50px 1fr 56px 64px 56px;padding:8px 0;border-bottom:1px solid #f1f5f9;font-size:12px;align-items:center}
.dir-badge{width:18px;height:18px;border-radius:4px;display:inline-flex;align-items:center;justify-content:center;font-size:9px;font-weight:700}
.result-pill{font-weight:700;font-size:10px;padding:2px 6px;border-radius:4px;display:inline-block}
.arb-card{background:#fff;border-radius:10px;border:2px solid #fde68a;padding:14px 16px}
.arb-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:12px}
.arb-stat{background:#fffbeb;border-radius:8px;padding:8px 10px;text-align:center}
.arb-stat .al{font-size:9px;color:#92400e;font-weight:600;text-transform:uppercase;letter-spacing:.03em;margin-bottom:2px}
.arb-stat .av{font-size:18px;font-weight:800;color:#92400e}
.arb-stat .as{font-size:9px;color:#b45309;margin-top:2px}
.arb-mhdr{display:grid;grid-template-columns:42px 1fr 52px 52px 56px 48px 42px;font-size:9px;font-weight:600;color:#94a3b8;padding:0 0 5px;border-bottom:1.5px solid #e2e8f0;letter-spacing:.03em;text-transform:uppercase;gap:4px}
.arb-mrow{display:grid;grid-template-columns:42px 1fr 52px 52px 56px 48px 42px;padding:5px 0;border-bottom:1px solid #f1f5f9;font-size:11px;align-items:center;gap:4px}
.tf-pill{font-size:8px;font-weight:700;padding:1px 5px;border-radius:3px;display:inline-block;white-space:nowrap}
@media(max-width:900px){.g5{grid-template-columns:1fr 1fr}.gm{grid-template-columns:1fr}.hs{grid-template-columns:1fr 1fr}.arb-grid{grid-template-columns:repeat(3,1fr)}}
"""

    HERO = """
<div class="hero">
  <div class="c1"></div><div class="c2"></div>
  <div style="position:relative;z-index:1;max-width:1100px;margin:0 auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px">
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:18px;font-weight:800">BTC-15M</span>
        <span style="padding:2px 8px;border-radius:12px;background:rgba(255,255,255,.15);font-size:10px;font-weight:700;letter-spacing:.04em">ORACLE</span>
        <div id="sb" style="display:flex;align-items:center;gap:4px;padding:2px 10px;border-radius:12px;background:rgba(255,200,50,.3);margin-left:6px">
          <div id="sd" style="width:6px;height:6px;border-radius:50%;background:#fff"></div>
          <span id="st" style="font-size:10px;font-weight:700">CONNECTING</span>
        </div>
      </div>
      <div id="ci" style="font-size:11px;opacity:.7">Connecting to bot...</div>
    </div>
    <div style="text-align:center;margin-bottom:16px">
      <div style="font-size:12px;font-weight:500;opacity:.8;margin-bottom:4px">Total Profit &middot; Cycle <span id="hc">0</span></div>
      <div id="hp" style="font-size:42px;font-weight:800;line-height:1">$0.00</div>
      <div style="font-size:12px;opacity:.7;margin-top:6px"><span id="ht">0</span> Bets &middot; $<span id="hw">0.00</span> Wagered</div>
    </div>
    <div class="hs">
      <div><div class="hl">Win Rate</div><div class="hv" id="hwr">0%</div></div>
      <div><div class="hl">BTC (Chainlink)</div><div class="hv" id="hbtc">$0</div></div>
      <div><div class="hl">Next Entry</div><div class="hv" id="htm" style="color:#16a34a">--:--</div></div>
      <div><div class="hl">Avg P&L / Bet</div><div class="hv" id="hav">$0.00</div></div>
    </div>
  </div>
</div>
"""

    STAT_CARDS = """
  <div class="g5">
    <div class="sc" style="border:2px solid #bbf7d0"><div class="ac" style="background:#16a34a"></div><div class="lb">Bankroll</div><div class="vl" id="sb1">$1,000</div><div class="sb">Available balance</div></div>
    <div class="sc" id="spc" style="border:2px solid #bbf7d0"><div class="ac" id="spa" style="background:#16a34a"></div><div class="lb">Realized P&L</div><div class="vl" id="sp" style="color:#16a34a">+$0.00</div><div class="sb" id="swl">0W &ndash; 0L</div></div>
    <div class="sc" style="border:2px solid #c7d2fe"><div class="ac" style="background:#6366f1"></div><div class="lb">Window Open</div><div class="vl" id="so">&mdash;</div><div class="sb" id="sdr">Waiting...</div></div>
    <div class="sc" style="border:2px solid #e2e8f0"><div class="lb">Strategy</div><div class="vl" id="sdi" style="color:#94a3b8">HOLD</div><div class="sb" id="scf">Confidence: 0%</div></div>
    <div class="sc" style="border:2px solid #fde68a"><div class="ac" style="background:#f59e0b"></div><div class="lb">Daily Risk</div><div class="vl" id="sr" style="color:#f59e0b">0/20</div><div class="sb" id="ss">Streak: 0</div></div>
  </div>
"""

    ARB_PANEL = """
      <!-- Arb Scanner Panel -->
      <div class="arb-card" id="arb-panel" style="display:none">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:13px;font-weight:700;color:#92400e">&#9889; Arb Scanner</span>
            <span id="arb-status" style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:12px;background:#fef3c7;color:#b45309">SCANNING</span>
          </div>
          <div style="font-size:10px;color:#b45309;font-family:'DM Mono',monospace">
            Scan #<span id="arb-scan-count">0</span> &middot; <span id="arb-scan-ms">0</span>ms
          </div>
        </div>
        <div class="arb-grid">
          <div class="arb-stat"><div class="al">Markets Live</div><div class="av" id="arb-mkts">0</div><div class="as" id="arb-tf-breakdown">-</div></div>
          <div class="arb-stat"><div class="al">Arb Trades</div><div class="av" id="arb-trades">0</div><div class="as" id="arb-trade-limit">/ 50 daily</div></div>
          <div class="arb-stat"><div class="al">Arb Profit</div><div class="av" id="arb-profit">$0</div><div class="as" id="arb-spent">$0 committed</div></div>
          <div class="arb-stat"><div class="al">Budget Left</div><div class="av" id="arb-budget">$200</div><div class="as" id="arb-budget-total">of $200/day</div></div>
          <div class="arb-stat"><div class="al">Best Edge</div><div class="av" id="arb-best-edge">0%</div><div class="as">today</div></div>
        </div>
        <!-- Live Markets Table -->
        <div style="font-size:11px;font-weight:700;color:#334155;margin-bottom:6px">Live BTC Markets</div>
        <div class="arb-mhdr">
          <span>TF</span><span>Market</span><span style="text-align:right">YES</span><span style="text-align:right">NO</span><span style="text-align:right">Sum</span><span style="text-align:right">Liq</span><span style="text-align:right">Time</span>
        </div>
        <div id="arb-markets-body" style="max-height:200px;overflow-y:auto">
          <div style="padding:20px;text-align:center;color:#94a3b8;font-size:11px">Discovering markets...</div>
        </div>
        <!-- Near Misses -->
        <div id="arb-near-misses" style="margin-top:8px;display:none">
          <div style="font-size:10px;color:#94a3b8;font-weight:600;margin-bottom:4px">NEAR MISSES (within 2% of threshold)</div>
          <div id="arb-near-body" style="font-size:10px;color:#b45309;font-family:'DM Mono',monospace"></div>
        </div>
      </div>
"""

    POSITIONS = """
      <div class="card" style="flex:1">
        <div style="display:flex;gap:0;margin-bottom:10px">
          <button class="tab-btn active" id="tab-open" onclick="switchTab('open')">Open (<span id="open-count">0</span>)</button>
          <button class="tab-btn" id="tab-closed" onclick="switchTab('closed')">History (<span id="closed-count">0</span>)</button>
        </div>
        <div class="tbl-hdr">
          <span>Time</span><span>Dir</span><span>Size</span><span style="text-align:right">Conf</span><span style="text-align:right">P&L</span><span style="text-align:right" id="col-last">Exp</span>
        </div>
        <div id="positions-body" style="max-height:240px;overflow-y:auto">
          <div style="padding:32px;text-align:center;color:#94a3b8;font-size:12px">Waiting for entry window...</div>
        </div>
      </div>
"""

    RIGHT_SIDEBAR = """
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="card"><div style="font-size:12px;font-weight:700;color:#334155;margin-bottom:6px">Signals</div><div id="sg"><div style="padding:16px;text-align:center;color:#94a3b8;font-size:11px">Waiting for first cycle...</div></div><div style="margin-top:8px;font-size:10px;color:#94a3b8;line-height:1.5">Price vs Open: <span style="font-weight:700;color:#6366f1">35% weight</span> &middot; Chainlink BTC/USD</div></div>
      <div class="card"><div style="font-size:12px;font-weight:700;color:#334155;margin-bottom:8px">Engines</div><div id="en"></div></div>
      <div class="card"><div style="font-size:12px;font-weight:700;color:#334155;margin-bottom:8px">Oracle Sources</div><div id="os"></div><div style="margin-top:6px;font-size:10px;color:#94a3b8">Spread: <span id="osp">0</span>%</div></div>
    </div>
"""

    JS = r"""
let ws,state,eq=[1000],curTab='open';
function timer(){const n=new Date(),m=n.getMinutes(),s=n.getSeconds(),t=Math.max(0,(((Math.floor(m/15)+1)*15-m-1)*60+(60-s))%900-60);return{text:`${Math.floor(t/60)}:${String(t%60).padStart(2,'0')}`,secs:t,color:t<60?'#dc2626':t<240?'#f59e0b':'#16a34a'}}

function switchTab(tab){
  curTab=tab;
  document.getElementById('tab-open').className='tab-btn'+(tab==='open'?' active':'');
  document.getElementById('tab-closed').className='tab-btn'+(tab==='closed'?' active':'');
  document.getElementById('col-last').textContent=tab==='closed'?'Result':'Exp';
  if(state)renderPositions(state);
}

function fmtTime(ts){if(!ts)return'--:--';const d=new Date(typeof ts==='number'?ts*1000:ts);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function fmtShortTime(ts){if(!ts)return'--:--';const d=new Date(typeof ts==='number'?ts*1000:ts);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}

function tradeRow(p,closed){
  const up=p.direction==='up'||p.direction==='UP'||p.d==='UP';
  const dir=(p.direction||p.d||'').toUpperCase();
  const sz=p.size_usd||p.sz||0;
  const ep=p.entry_price||p.ep||0;
  const conf=p.confidence||p.conf||0;
  const pnl=closed?(p.pnl||0):(p.uPnl||0);
  const pc=pnl>=0?'#16a34a':'#dc2626';
  const time=closed?fmtTime(p.timestamp||p.ct):fmtTime(p.timestamp||p.t);
  let last='';
  if(closed){
    const win=p.outcome==='win'||p.win===true||pnl>0;
    last=`<span class="result-pill" style="background:${win?'#dcfce7':'#fee2e2'};color:${win?'#16a34a':'#dc2626'}">${win?'WIN':'LOSS'}</span>`;
  }else{
    last=`<span style="font-size:10px;color:#f59e0b;font-weight:600">${fmtShortTime(p.expiry||p.exp)}</span>`;
  }
  return `<div class="tbl-row">
    <span style="color:#94a3b8">${time}</span>
    <span style="display:inline-flex;align-items:center;gap:4px">
      <span class="dir-badge" style="background:${up?'#dcfce7':'#fee2e2'};color:${up?'#16a34a':'#dc2626'}">${up?'&#9650;':'&#9660;'}</span>
      <span style="font-weight:600;color:#334155;font-size:11px">${dir}</span>
    </span>
    <span style="color:#64748b">$${parseFloat(sz).toFixed(2)} <span style="color:#cbd5e1">@</span> ${parseFloat(ep).toFixed(3)}</span>
    <span style="color:#94a3b8;text-align:right">${(conf*100).toFixed(0)}%</span>
    <span style="font-weight:700;color:${pc};text-align:right">${pnl>=0?'+':''}${parseFloat(pnl).toFixed(2)}</span>
    <span style="text-align:right">${last}</span>
  </div>`;
}

function renderPositions(d){
  const op=d.positions?.open||[];
  const cl=d.positions?.closed||[];
  document.getElementById('open-count').textContent=op.length;
  document.getElementById('closed-count').textContent=cl.length;
  const body=document.getElementById('positions-body');
  const items=curTab==='open'?op:cl;
  if(items.length===0){
    body.innerHTML=`<div style="padding:32px;text-align:center;color:#94a3b8;font-size:12px">${curTab==='open'?'Waiting for entry window...':'No history yet'}</div>`;
  }else{
    body.innerHTML=items.map(p=>tradeRow(p,curTab==='closed')).join('');
  }
}

// ── Arb Scanner Rendering ──
const TF_COLORS={'5m':'#9333ea','15m':'#2563eb','30m':'#059669','1h':'#dc2626'};
function fmtSecs(s){if(!s||s<=0)return'--';if(s<60)return Math.round(s)+'s';if(s<3600)return Math.round(s/60)+'m';return(s/3600).toFixed(1)+'h'}

function renderArb(d){
  const a=d.arb_scanner;
  const panel=document.getElementById('arb-panel');
  if(!a){panel.style.display='none';return}
  panel.style.display='block';
  document.getElementById('arb-scan-count').textContent=a.scan_count||0;
  document.getElementById('arb-scan-ms').textContent=a.scan_time_ms||0;
  document.getElementById('arb-status').textContent=a.running?'SCANNING':'STOPPED';
  document.getElementById('arb-status').style.background=a.running?'#fef3c7':'#fee2e2';
  document.getElementById('arb-status').style.color=a.running?'#b45309':'#dc2626';
  document.getElementById('arb-mkts').textContent=a.markets_live||0;
  // Timeframe breakdown
  const tf=a.markets_by_timeframe||{};
  const tfStr=Object.entries(tf).map(([k,v])=>`${k}:${v}`).join(' ');
  document.getElementById('arb-tf-breakdown').textContent=tfStr||'-';
  document.getElementById('arb-trades').textContent=a.daily_trades||0;
  document.getElementById('arb-trade-limit').textContent=`/ ${a.daily_max_trades||50} daily`;
  document.getElementById('arb-profit').textContent=`$${(a.daily_profit||0).toFixed(2)}`;
  document.getElementById('arb-spent').textContent=`$${(a.daily_spent||0).toFixed(2)} spent`;
  document.getElementById('arb-budget').textContent=`$${(a.daily_budget_remaining||0).toFixed(0)}`;
  document.getElementById('arb-budget-total').textContent=`of $${(a.daily_budget||200).toFixed(0)}/day`;
  document.getElementById('arb-best-edge').textContent=`${(a.best_edge_pct||0).toFixed(1)}%`;
  // Market list
  const mkts=a.market_list||[];
  const body=document.getElementById('arb-markets-body');
  if(mkts.length===0){
    body.innerHTML='<div style="padding:20px;text-align:center;color:#94a3b8;font-size:11px">No BTC markets found. Markets appear during trading hours.</div>';
  }else{
    body.innerHTML=mkts.map(m=>{
      const c=TF_COLORS[m.timeframe]||'#64748b';
      const sum=m.combined||0;
      const isArb=m.is_arb;
      const sumC=isArb?'#16a34a':sum===0?'#94a3b8':'#334155';
      const bg=isArb?'background:#f0fdf4;':'';
      const q=m.question.replace(/Bitcoin Up or Down - /,'').replace(/,?\s*\d{4}/,'');
      return `<div class="arb-mrow" style="${bg}">
        <span><span class="tf-pill" style="background:${c}15;color:${c}">${m.tf_label}</span></span>
        <span style="color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${m.question}">${q}</span>
        <span style="text-align:right;color:#334155;font-weight:500">${m.price_yes?m.price_yes.toFixed(3):'-'}</span>
        <span style="text-align:right;color:#334155;font-weight:500">${m.price_no?m.price_no.toFixed(3):'-'}</span>
        <span style="text-align:right;font-weight:700;color:${sumC}">${sum?sum.toFixed(3):'-'}</span>
        <span style="text-align:right;color:#94a3b8;font-size:10px">$${(m.liquidity||0).toFixed(0)}</span>
        <span style="text-align:right;color:#f59e0b;font-size:10px;font-weight:600">${fmtSecs(m.time_remaining)}</span>
      </div>`}).join('');
  }
  // Near misses
  const nm=a.near_misses||[];
  const nmPanel=document.getElementById('arb-near-misses');
  if(nm.length>0){
    nmPanel.style.display='block';
    document.getElementById('arb-near-body').innerHTML=nm.map(n=>`${n.timeframe} | sum=${n.combined} | gap=${n.gap}%`).join('<br>');
  }else{
    nmPanel.style.display='none';
  }
}

function drawEq(){const c=document.getElementById('ec');if(!c)return;const x=c.getContext('2d'),w=c.offsetWidth,h=65;c.width=w*2;c.height=h*2;x.scale(2,2);if(eq.length<2)return;const mn=Math.min(...eq),mx=Math.max(...eq),r=mx-mn||1,up=eq[eq.length-1]>=eq[0],cl=up?'#16a34a':'#dc2626';x.beginPath();x.moveTo(0,h);for(let i=0;i<eq.length;i++){x.lineTo((i/(eq.length-1))*w,h-((eq[i]-mn)/r)*(h-8)-4)}x.lineTo(w,h);x.closePath();const g=x.createLinearGradient(0,0,0,h);g.addColorStop(0,up?'rgba(22,163,74,.15)':'rgba(220,38,38,.15)');g.addColorStop(1,'rgba(0,0,0,0)');x.fillStyle=g;x.fill();x.beginPath();for(let i=0;i<eq.length;i++){const px=(i/(eq.length-1))*w,py=h-((eq[i]-mn)/r)*(h-8)-4;i===0?x.moveTo(px,py):x.lineTo(px,py)}x.strokeStyle=cl;x.lineWidth=2;x.stroke()}

function render(d){if(!d)return;const p=d.stats?.total_pnl||0,pc=p>=0?'#16a34a':'#dc2626',wr=d.stats?.win_rate||0,bk=d.config?.bankroll||1000,dr=d.anchor?.drift_pct??d.strategy?.drift_pct,op=d.anchor?.open_price,t=timer();
eq.push(bk);if(eq.length>200)eq.shift();
document.getElementById('hc').textContent=d.cycle||0;document.getElementById('hp').textContent=`${p>=0?'+':''}$${p.toFixed(2)}`;document.getElementById('ht').textContent=d.stats?.total_trades||0;document.getElementById('hw').textContent=d.stats?.total_wagered||'0.00';document.getElementById('hwr').textContent=`${wr}%`;document.getElementById('hbtc').textContent=`$${parseFloat(d.oracle?.price||0).toLocaleString()}`;const te=document.getElementById('htm');te.textContent=t.text;te.style.color=t.color;const av=d.stats?.total_trades>0?p/d.stats.total_trades:0;document.getElementById('hav').textContent=`$${av.toFixed(2)}`;
document.getElementById('sb1').textContent=`$${typeof bk==='number'?bk.toFixed(2):bk}`;const se=document.getElementById('sp');se.textContent=`${p>=0?'+':''}$${p.toFixed(2)}`;se.style.color=pc;document.getElementById('spa').style.background=pc;document.getElementById('spc').style.borderColor=p>=0?'#bbf7d0':'#fecaca';document.getElementById('swl').textContent=`${d.stats?.wins||0}W \u2013 ${d.stats?.losses||0}L`;document.getElementById('so').textContent=op?`$${parseFloat(op).toLocaleString()}`:'\u2014';document.getElementById('sdr').textContent=dr!=null?`Drift: ${dr>0?'+':''}${parseFloat(dr).toFixed(4)}%`:'Waiting...';const di=document.getElementById('sdi');di.textContent=(d.strategy?.direction||'hold').toUpperCase();di.style.color=d.strategy?.direction==='up'?'#16a34a':d.strategy?.direction==='down'?'#dc2626':'#94a3b8';document.getElementById('scf').textContent=`Confidence: ${((d.strategy?.confidence||0)*100).toFixed(1)}%`;document.getElementById('sr').textContent=`${d.risk?.daily_trades||0}/${d.risk?.max_daily_trades||20}`;document.getElementById('ss').textContent=`Streak: ${d.risk?.consecutive_losses||0}`;
const epl=document.getElementById('ep');epl.textContent=`${p>=0?'+':''}$${p.toFixed(2)}`;epl.style.color=pc;drawEq();
renderPositions(d);
renderArb(d);
// Signals
const sigs=d.signals||{},sk=Object.keys(sigs);if(sk.length>0){document.getElementById('sg').innerHTML=sk.map(n=>{const s=sigs[n],u=s.direction==='up',c=u?'#16a34a':'#dc2626',l=n.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase()),x=n==='rsi'?`RSI: ${s.raw_value}`:n==='price_vs_open'?`Drift: ${s.raw_value>0?'+':''}${s.raw_value}%`:'';return`<div class="sr"><div class="si ${u?'su':'sd'}">${u?'\u25b2':'\u25bc'}</div><div style="flex:1"><div style="font-size:12px;font-weight:600;color:#334155">${l}</div>${x?`<div style="font-size:10px;color:#94a3b8">${x}</div>`:''}</div><div style="font-size:13px;font-weight:700;color:${c}">${s.direction.toUpperCase()} ${Math.round(s.strength*100)}%</div></div>`}).join('')}
document.getElementById('en').innerHTML=[{n:'Directional',on:true,c:'#16a34a'},{n:'Arbitrage',on:d.config?.arb_enabled,c:'#f59e0b'},{n:'Hedge',on:d.config?.hedge_enabled,c:'#6366f1'}].map(m=>`<div class="er"><div class="dot" style="background:${m.on?m.c:'#e2e8f0'}"></div><span style="flex:1;font-size:12px;font-weight:500;color:${m.on?'#334155':'#94a3b8'}">${m.n}</span><span class="bg" style="background:${m.on?m.c+'15':'#f1f5f9'};color:${m.on?m.c:'#94a3b8'}">${m.on?'ACTIVE':'OFF'}</span></div>`).join('');
document.getElementById('os').innerHTML=(d.oracle?.sources||[]).map(s=>`<div style="display:flex;align-items:center;gap:6px;padding:4px 0"><div class="dot" style="background:#16a34a;width:6px;height:6px"></div><span style="font-size:11px;color:#334155;font-weight:${s==='chainlink'?700:400}">${s}${s==='chainlink'?' (resolution)':''}</span></div>`).join('');
document.getElementById('osp').textContent=d.oracle?.spread_pct||0}

function conn(){const p=location.protocol==='https:'?'wss':'ws';ws=new WebSocket(`${p}://${location.host}/ws`);ws.onopen=()=>{document.getElementById('sb').style.background='rgba(255,255,255,.2)';document.getElementById('sd').style.animation='pulse 2s infinite';document.getElementById('st').textContent='LIVE';document.getElementById('ci').textContent='Connected'};ws.onmessage=e=>{try{state=JSON.parse(e.data);render(state)}catch{}};ws.onclose=()=>{document.getElementById('sb').style.background='rgba(255,200,50,.3)';document.getElementById('sd').style.animation='none';document.getElementById('st').textContent='RECONNECTING';document.getElementById('ci').textContent='Retrying...';setTimeout(conn,3000)};ws.onerror=()=>ws.close()}
setInterval(()=>{const t=timer();const e=document.getElementById('htm');if(e){e.textContent=t.text;e.style.color=t.color}},1000);
conn();drawEq();
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC-15M-Oracle Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
{HERO}
<div style="max-width:1100px;margin:0 auto;padding:18px 24px">
{STAT_CARDS}
  <div class="gm">
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="card"><div style="display:flex;justify-content:space-between;margin-bottom:8px"><span style="font-size:12px;font-weight:700;color:#334155">Equity Curve</span><span class="mono" style="font-size:12px;font-weight:700" id="ep">+$0.00</span></div><canvas id="ec" height="65" style="width:100%;display:block"></canvas></div>
{ARB_PANEL}
{POSITIONS}
    </div>
{RIGHT_SIDEBAR}
  </div>
  <div style="margin-top:16px;text-align:center;font-size:10px;color:#cbd5e1">BTC-15M-ORACLE v2.0 &middot; Chainlink resolution &middot; entries :59/:14/:29/:44</div>
</div>
<script>{JS}</script>
</body>
</html>"""
