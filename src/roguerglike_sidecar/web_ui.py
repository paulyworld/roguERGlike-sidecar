"""aiohttp UI for mock mode: three sliders, one page, no build step."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiohttp import web

from .mock import MockState

log = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8422

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>roguERGlike sidecar - mock</title>
<style>
  body { font: 16px system-ui, sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.2rem; margin-bottom: 1.5rem; }
  .row { display: grid; grid-template-columns: 5rem 1fr 4rem; gap: .75rem;
         align-items: center; margin: 1rem 0; }
  label { font-weight: 600; }
  output { font-variant-numeric: tabular-nums; text-align: right; }
  input[type=range] { width: 100%; }
  .hint { color: #666; font-size: .85rem; }
</style>
</head>
<body>
  <h1>Mock trainer</h1>
  <p class="hint">Events stream to ws://localhost:8421. Move a slider; the engine
     will see the new value within ~1 s.</p>

  <div class="row">
    <label for="watts">Power</label>
    <input id="watts" type="range" min="0" max="600" value="0">
    <output for="watts" id="watts_out">0 W</output>
  </div>

  <div class="row">
    <label for="rpm">Cadence</label>
    <input id="rpm" type="range" min="0" max="140" value="0">
    <output for="rpm" id="rpm_out">0 rpm</output>
  </div>

  <div class="row">
    <label for="bpm">Heart rate</label>
    <input id="bpm" type="range" min="40" max="200" value="60">
    <output for="bpm" id="bpm_out">60 bpm</output>
  </div>

<script>
  const fmt = { watts: v => v + " W", rpm: v => v + " rpm", bpm: v => v + " bpm" };
  let pending = {};
  let inflight = false;

  async function flush() {
    if (inflight || Object.keys(pending).length === 0) return;
    inflight = true;
    const body = pending; pending = {};
    try {
      await fetch("/control", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify(body),
      });
    } catch (e) { console.warn(e); }
    inflight = false;
    if (Object.keys(pending).length > 0) flush();
  }

  for (const id of ["watts", "rpm", "bpm"]) {
    const el = document.getElementById(id);
    const out = document.getElementById(id + "_out");
    el.addEventListener("input", () => {
      out.textContent = fmt[id](el.value);
      pending[id] = Number(el.value);
      flush();
    });
  }
</script>
</body>
</html>
"""


async def _index(_request: web.Request) -> web.Response:
    return web.Response(text=_PAGE, content_type="text/html")


async def _control(request: web.Request) -> web.Response:
    state: MockState = request.app["mock_state"]
    payload = await request.json()
    if "watts" in payload:
        await state.set_watts(int(payload["watts"]))
    if "rpm" in payload:
        await state.set_rpm(int(payload["rpm"]))
    if "bpm" in payload:
        await state.set_bpm(int(payload["bpm"]))
    return web.json_response({"ok": True})


def build_app(state: MockState) -> web.Application:
    app = web.Application()
    app["mock_state"] = state
    app.router.add_get("/", _index)
    app.router.add_post("/control", _control)
    return app


@asynccontextmanager
async def run_web_ui(
    state: MockState,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> AsyncIterator[web.AppRunner]:
    app = build_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("sidecar UI on http://%s:%d", host, port)
    try:
        yield runner
    finally:
        await runner.cleanup()
