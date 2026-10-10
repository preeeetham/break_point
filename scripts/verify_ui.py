#!/usr/bin/env python3
"""Visual + console verification for the control room.

Loads the dashboard in a real Chrome, fails on console errors or failed requests, screenshots the
documented breakpoints and reports anything that overflows horizontally.

    .venv/bin/python scripts/verify_ui.py --url http://127.0.0.1:8777 --out /tmp/faultline-ui
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BREAKPOINTS = [("desktop", 1920, 1080), ("laptop", 1440, 900), ("tablet", 1024, 820),
               ("narrow", 820, 1180), ("phone", 390, 844)]


def main() -> int:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8777")
    ap.add_argument("--out", default="/tmp/faultline-ui")
    ap.add_argument("--settle", type=float, default=3.5, help="seconds to let polling and modules settle")
    ap.add_argument("--expand", action="store_true", help="also expand the first experiment row")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for name, w, h in BREAKPOINTS:
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2,
                                      color_scheme="dark")
            page = ctx.new_page()
            errors: list[str] = []
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                    if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("requestfailed", lambda r: errors.append(f"requestfailed: {r.url} {r.failure}"))
            page.goto(args.url, wait_until="networkidle")
            page.wait_for_timeout(int(args.settle * 1000))
            if args.expand and name == "desktop":
                row = page.query_selector(".exp-row")
                if row:
                    row.click()
                    page.wait_for_timeout(600)

            metrics = page.evaluate("""() => {
                const de = document.documentElement;
                const over = [...document.querySelectorAll('*')]
                  .filter(n => n.scrollWidth > n.clientWidth + 2 && getComputedStyle(n).overflowX === 'visible')
                  .slice(0, 8)
                  .map(n => `${n.tagName.toLowerCase()}.${(n.className||'').toString().split(' ')[0]} ${n.scrollWidth}>${n.clientWidth}`);
                const tiny = [...document.querySelectorAll('button, a, input, select')]
                  .filter(n => { const r = n.getBoundingClientRect(); const cs = getComputedStyle(n);
                                 // skip visually-hidden controls: the visible label is the real target
                                 if (cs.opacity === '0' || cs.clipPath !== 'none' || cs.position === 'absolute' && r.width <= 2) return false;
                                 return r.width > 2 && r.height > 2 && (r.height < 32 || r.width < 24); })
                  .slice(0, 8)
                  .map(n => `${n.tagName.toLowerCase()}.${(n.className||'').toString().split(' ')[0]} ${Math.round(n.getBoundingClientRect().width)}x${Math.round(n.getBoundingClientRect().height)}`);
                return {
                  docOverflow: de.scrollWidth > de.clientWidth + 1 ? `${de.scrollWidth}>${de.clientWidth}` : null,
                  overflowing: over, smallTargets: tiny,
                  panels: document.querySelectorAll('.panel').length,
                  svgNodes: document.querySelectorAll('#graph-host svg').length,
                  canvases: document.querySelectorAll('canvas').length,
                  launcher: document.querySelectorAll('#launcher-host *').length,
                  feedRows: document.querySelectorAll('.feed__row').length,
                  expRows: document.querySelectorAll('.exp-row').length,
                  emptyBody: document.body.innerText.trim().length < 80,
                };
            }""")
            page.screenshot(path=str(out / f"{name}.png"), full_page=(name in ("narrow", "phone")))
            ctx.close()

            tag = f"[{name} {w}x{h}]"
            if metrics["docOverflow"]:
                problems.append(f"{tag} horizontal scroll: {metrics['docOverflow']}")
            for o in metrics["overflowing"]:
                problems.append(f"{tag} overflowing element: {o}")
            for t in metrics["smallTargets"]:
                problems.append(f"{tag} touch target under 32px: {t}")
            if metrics["emptyBody"]:
                problems.append(f"{tag} page rendered essentially empty")
            for e in errors[:12]:
                problems.append(f"{tag} {e}")
            print(f"{tag} panels={metrics['panels']} graphSvg={metrics['svgNodes']} "
                  f"canvas={metrics['canvases']} launcherNodes={metrics['launcher']} "
                  f"feed={metrics['feedRows']} experiments={metrics['expRows']}", flush=True)
        browser.close()

    print(f"\nscreenshots -> {out}")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p_ in problems:
            print(f"  - {p_}")
        return 1
    print("\nno console errors, no overflow, no undersized targets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
