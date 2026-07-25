"""Assemble the study report from search_results.json.

Reports survivors if any, and near-misses always -- a run where nothing clears
the bar is a result, and the near-miss table is the deliverable in that case.

The calibration caveats are reproduced in the report body rather than filed
away, because every number downstream inherits them.
"""

from __future__ import annotations

import json

import numpy as np

from research import gauntlet, sizing

CAVEATS = """\
> **Read these before the numbers.** Every result below inherits them.
>
> - The option overlay passed its credibility gate at **11 of 22 contracts**,
>   which is exactly the threshold. That is a threshold pass, not a strong one.
> - Validation rests on a **limited real option window**: Delta purges option
>   candle history ~2 days after expiry, so the calibration and validation set
>   is a few days wide, not months.
> - The overlay validates unevenly by maturity: **5/5 above 7d, 4/9 at 1-3d,
>   2/8 below a day**. Results are reported by DTE band so each can be weighted
>   accordingly. Sub-daily is EXPLORATORY and is not part of the acceptance set.
> - `research/record_book.py` can collect live bid/ask snapshots, which in
>   30-60 days give a genuinely executable, non-selected validation target and
>   remove most of the above. Do NOT assume it is running: an earlier note
>   claimed continuous collection when only a single smoke-test snapshot had
>   been taken. Verify the loop process and that the daily file is GROWING
>   before treating the data as a validation set.
"""


def _fmt(x, n=4):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{n}f}"
    return str(x)


def _near_misses(results, k=12):
    """Ranked by how far through the gauntlet they got, then by expectancy."""
    losers = [r for r in results if not r.get("passed")]
    losers.sort(key=lambda r: (-(r.get("reached") or -1),
                               -(r.get("expectancy") or -9e9)))
    return losers[:k]


def _section(block) -> list[str]:
    L = []
    res = block["results"]
    surv = block["survivors"]
    L.append(f"### {block['label']}\n")
    L.append(f"- Declared search budget: **{block['budget']}** candidates "
             f"(multiple-comparison correction applied over all of them)")
    L.append(f"- RNG seed: `{block['seed']}`   DTE bands: `{block['dte_bands']}`")
    L.append(f"- Evaluated: {len(res)}   **Survivors: {len(surv)}**   "
             f"Errors: {block.get('n_errors', 0)}\n")

    L.append("**Where candidates died:**\n")
    L.append("| criterion | candidates |")
    L.append("|---|---|")
    died = block.get("died_at", {})
    for name in gauntlet.ORDER + ["error"]:
        if died.get(name):
            L.append(f"| `{name}` | {died[name]} |")
    L.append("")

    if surv:
        L.append("**Survivors:**\n")
        L.append("| family | TF | DTE | gates | trades | expectancy | WF | shuffle p | corrected p | holdout |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in surv:
            wf = ",".join(_fmt(x, 3) for x in (r.get("walk_forward") or []))
            L.append(f"| {r['family']} | {r['timeframe']} | {r['dte_band']} | "
                     f"{'+'.join(r['gates']) or '-'} | {r.get('n_trades')} | "
                     f"{_fmt(r.get('expectancy'))} | {wf} | "
                     f"{_fmt(r.get('shuffle_p'),3)} | {_fmt(r.get('corrected_p'),4)} | "
                     f"{_fmt(r.get('holdout'))} |")
        L.append("")
    else:
        L.append("**No candidate cleared the bar.**\n")

    L.append("**Closest near-misses** (ranked by how far through the gauntlet "
             "they got, then by expectancy):\n")
    L.append("| family | TF | DTE | gates | trades | expectancy | died at |")
    L.append("|---|---|---|---|---|---|---|")
    for r in _near_misses(res):
        L.append(f"| {r['family']} | {r['timeframe']} | {r.get('dte_band','-')} | "
                 f"{'+'.join(r.get('gates') or []) or '-'} | {r.get('n_trades','-')} | "
                 f"{_fmt(r.get('expectancy'))} | `{r.get('failed_at')}` |")
    L.append("")

    # by DTE band and timeframe, so the reader can weight by overlay quality
    L.append("**Best expectancy by DTE band and timeframe:**\n")
    L.append("| DTE band | timeframe | best expectancy | n candidates |")
    L.append("|---|---|---|---|")
    for band in sorted({r.get("dte_band") for r in res if r.get("dte_band")}):
        for tf in sorted({r["timeframe"] for r in res}):
            sel = [r for r in res if r.get("dte_band") == band
                   and r["timeframe"] == tf and r.get("expectancy") is not None]
            if sel:
                best = max(sel, key=lambda r: r["expectancy"])
                L.append(f"| {band} | {tf} | {_fmt(best['expectancy'])} "
                         f"({best['family']}) | {len(sel)} |")
    L.append("")
    return L


def build(path="research/search_results.json") -> str:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)

    L = ["# BTC Options Strategy Study", ""]
    L.append(f"Generated: {d.get('generated','-')}")
    L.append("")
    L.append(CAVEATS)
    L.append("")

    prim = d["primary"]
    L.append("## Verdict\n")
    if prim["survivors"]:
        L.append(f"**{len(prim['survivors'])} of {prim['budget']} candidates "
                 f"cleared the Strict bar** in the primary (DTE >= 1d) scope.")
    else:
        L.append(f"**No candidate cleared the Strict bar.** "
                 f"{prim['budget']} were tested in the primary (DTE >= 1d) scope. "
                 "The near-miss table below records how far each got and which "
                 "criterion stopped it.")
    L.append("")
    L.append("The bar was fixed before the search and was NOT relaxed after the "
             "overlay passed its own gate narrowly: min 200 trades, positive "
             "out-of-sample expectancy, positive in >=3 of 4 walk-forward "
             "windows, beats its direction-shuffled null at p<0.05, survives "
             "Bonferroni correction over the full declared budget, stays "
             "positive across the overlay's +-15% IV error band, and is "
             "net-positive on a holdout touched exactly once.\n")

    L.append("## Results\n")
    L += _section(prim)
    L += _section(d["exploratory"])

    L.append("## Sizing\n")
    if prim["survivors"]:
        L.append("Sizing is derived from measured edge (quarter-Kelly, capped by "
                 "the 95th-percentile drawdown). See `sizing.recommend`.\n")
    else:
        L.append("**No strategy is validated, so recommended size is zero by "
                 "construction** (`sizing.recommend(..., validated=False)`). "
                 "The engine should ship with sizing disabled rather than with "
                 "tuned numbers implying confidence the evidence does not "
                 "support.\n")

    L.append("## Reproducibility\n")
    L.append(f"- Seed: `{prim['seed']}` (all shuffle/permutation tests)")
    L.append(f"- Declared budget: primary {prim['budget']}, "
             f"exploratory {d['exploratory']['budget']}")
    L.append("- Data: BTCUSD 1m, 365 days, 525,496 bars, 99.98% complete; "
             "higher timeframes resampled from that single series")
    L.append("- Fees: Delta options `min(0.01% notional, 3.5% premium) x 1.18`, "
             "verified live 2026-07-24")
    L.append(f"- Overlay fits: {json.dumps(prim['fits'].get('carry', {}), default=str)}")
    L.append("")
    return "\n".join(L)


def main():
    text = build()
    out = "research/STUDY_2026-07-25.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"wrote {out} ({len(text)} chars)")


if __name__ == "__main__":
    main()
