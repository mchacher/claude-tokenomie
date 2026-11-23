#!/usr/bin/env python3
"""claude_tokenomie : le coût de votre usage de Claude Code, en équivalent API.

Lit les journaux locaux de Claude Code (dossier .claude/projects de votre profil), additionne
les tokens et les valorise au tarif public de l'API Anthropic. Fonctionne avec le terminal et
avec l'extension VS Code, qui écrivent les mêmes journaux. Windows, macOS et Linux.

- Aucune dépendance : Python 3.9 ou plus récent suffit.
- Rien ne sort de votre poste. Le contenu de vos conversations n'est jamais lu.
- Le coût affiché est un équivalent API au tarif public. Ce n'est pas une facture :
  les sièges Claude Enterprise sont au forfait.

Exemples (sous Windows, remplacez python3 par py) :
    python3 claude_tokenomie.py                 # aujourd'hui, dans le terminal
    python3 claude_tokenomie.py --days 7        # 7 derniers jours, jour par jour
    python3 claude_tokenomie.py --watch         # suivi en direct dans le terminal
    python3 claude_tokenomie.py --web           # tableau de bord dans le navigateur
    python3 claude_tokenomie.py --json          # les mêmes chiffres, pour un autre outil
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PRICES_DATE = "21/11/2026"
# Tarif public, en $ par million de tokens :
# (entrée, sortie, écriture cache 5 min, écriture cache 1 h, lecture cache)
# Source : https://platform.claude.com/docs/en/about-claude/pricing
PRICES = {
    "fable-5-2": (8.0, 40.0, 10.0, 16.0, 0.20),
    "opus-5-5": (4.0, 20.0, 5.0, 8.0, 0.20),
    "sonnet-5-5": (2.0, 10.0, 2.5, 4.0, 0.10),
    "haiku-5-5": (0.10, 0.50, 0.125, 0.20, 0.01),
}
HAIKU_LONG = (0.50, 2.50, 0.625, 1.0, 0.05)  # Haiku 5.5, requêtes de plus de 100 000 tokens
PREMIUM = ("opus", "fable", "mythos")        # familles les plus chères, pour les conseils
FAMILIES = ("opus", "sonnet", "haiku", "fable", "mythos")
WEB_MAX_DAYS = 30
DISCLAIMER = "Équivalent API au tarif public, hors chat claude.ai. Ce n'est pas une facture (sièges au forfait)."


def setup_console():
    """Évite les plantages d'affichage sous Windows (console en cp1252) et active les couleurs ANSI."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        os.system("")   # active les séquences ANSI de la console Windows 10 et plus


class Agg:
    __slots__ = ("n", "cost", "inp", "out", "wr", "rd")

    def __init__(self):
        self.n = 0
        self.cost = self.inp = self.out = self.wr = self.rd = 0.0

    def add(self, cost, inp, out, wr, rd):
        self.n += 1
        self.cost += cost
        self.inp += inp
        self.out += out
        self.wr += wr
        self.rd += rd

    @property
    def tokens(self):
        return self.inp + self.out + self.wr + self.rd


class Store:
    """Lit les journaux en incrémental : en mode --watch ou --web, seuls les nouveaux octets sont relus."""

    def __init__(self, root, since):
        self.root, self.since = root, since
        self.offsets = {}
        self.msgs = {}       # id du message -> enregistrement (évite les doublons)
        self.cwd = {}        # dossier de projet -> chemin de travail le plus court vu

    def refresh(self):
        for f in self.root.rglob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime < self.since.timestamp():
                continue
            off = self.offsets.get(f, 0)
            if st.st_size <= off:
                continue
            rel = f.relative_to(self.root).parts
            proj = rel[0]
            side = "subagents" in rel
            sess = rel[1][:-6] if len(rel) == 2 else rel[1]
            try:
                with open(f, "rb") as fh:
                    fh.seek(off)
                    for line in fh:
                        if not line.endswith(b"\n"):   # ligne en cours d'écriture : on la relira
                            break
                        off += len(line)
                        self._line(line, proj, side, sess)
            except OSError:
                continue
            self.offsets[f] = off

    def _line(self, line, proj, side, sess):
        if b'"usage"' not in line:
            return
        try:
            d = json.loads(line)
        except ValueError:
            return
        if d.get("type") != "assistant":
            return
        m = d.get("message") or {}
        u, ts, mid = m.get("usage"), d.get("timestamp"), m.get("id")
        if not u or not ts or not mid:
            return
        if m.get("model", "").startswith("<"):   # message interne (<synthetic>) : aucun appel facturé
            return
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return
        cwd = d.get("cwd")
        if cwd and (proj not in self.cwd or len(cwd) < len(self.cwd[proj])):
            self.cwd[proj] = cwd
        cc = u.get("cache_creation") or {}
        w1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
        w5 = cc.get("ephemeral_5m_input_tokens", (u.get("cache_creation_input_tokens", 0) or 0) - w1) or 0
        self.msgs[mid] = dict(
            t=t, model=m.get("model", "?"), proj=proj, side=side, sess=sess,
            inp=u.get("input_tokens", 0) or 0, out=u.get("output_tokens", 0) or 0,
            w5=w5, w1=w1, rd=u.get("cache_read_input_tokens", 0) or 0,
            mult=(2.0 if u.get("speed") == "fast" else 1.0) * (1.1 if u.get("inference_geo") == "us" else 1.0),
        )

    def project_name(self, proj):
        cwd = self.cwd.get(proj)
        return re.split(r"[\\/]", cwd.rstrip("\\/"))[-1] if cwd else proj


def price_for(model, prices):
    for key, p in prices.items():
        if key in model:
            return key, p
    return None, None


def cost_of(r, prices):
    key, p = price_for(r["model"], prices)
    if p is None:
        return None
    if key == "haiku-5-5" and r["inp"] + r["w5"] + r["w1"] + r["rd"] > 100_000:
        p = HAIKU_LONG
    pi, po, p5, p1, pr = p
    return (r["inp"] * pi + r["out"] * po + r["w5"] * p5 + r["w1"] * p1 + r["rd"] * pr) / 1e6 * r["mult"]


def family(model):
    for f in FAMILIES:
        if f in model:
            return f
    return "autre"


def window(days):
    """Début (minuit local, il y a days-1 jours) et fin (maintenant) de la période."""
    today = dt.datetime.now().astimezone().date()
    start = dt.datetime.combine(today - dt.timedelta(days=max(days, 1) - 1), dt.time.min).astimezone()
    return start, dt.datetime.now().astimezone() + dt.timedelta(seconds=1)


def summarize(store, start, end, prices):
    S = dict(all=Agg(), day=defaultdict(Agg), hod=defaultdict(Agg), hours=set(), model=defaultdict(Agg),
             proj=defaultdict(Agg), side=defaultdict(Agg), sess=defaultdict(Agg), unpriced=defaultdict(int))
    for r in store.msgs.values():
        if not (start <= r["t"] < end):
            continue
        c = cost_of(r, prices)
        if c is None:
            S["unpriced"][r["model"]] += 1
            continue
        args = (c, r["inp"], r["out"], r["w5"] + r["w1"], r["rd"])
        S["all"].add(*args)
        S["day"][r["t"].date()].add(*args)
        S["hod"][r["t"].hour].add(*args)
        S["hours"].add((r["t"].date(), r["t"].hour))
        S["model"][r["model"]].add(*args)
        S["proj"][r["proj"]].add(*args)
        S["side"]["sous-agents" if r["side"] else "agent principal"].add(*args)
        S["sess"][(r["proj"], r["sess"])].add(*args)
    return S


def money(usd, rate):
    eur = usd * rate
    return f"{eur:,.1f} €" if eur < 10 else f"{eur:,.0f} €".replace(",", " ")


def tips(S, rate, budget, days):
    out = []
    a = S["all"]
    if not a.n:
        return out
    if budget and a.cost * rate > budget * days:
        out.append(f"Budget dépassé : {a.cost*rate:.0f} € pour {budget*days:.0f} € prévus.")
    prem = sum(g.cost for k, g in S["model"].items() if any(f in k for f in PREMIUM))
    if prem / a.cost >= 0.6 and a.cost * rate >= 5:
        out.append(f"{prem/a.cost:.0%} du coût part sur un modèle premium (Opus/Fable). Pour les tâches simples, "
                   "Sonnet coûte environ deux fois moins cher par token : essayez /model sonnet.")
    sub = S["side"].get("sous-agents")
    if sub and sub.cost / a.cost >= 0.5 and a.cost * rate >= 5:
        out.append(f"Les sous-agents pèsent {sub.cost/a.cost:.0%} du coût. Ils reprennent souvent le modèle de la "
                   "session : fixez `model: sonnet` dans leur définition pour les tâches de recherche ou de relecture.")
    ctx = (a.inp + a.wr + a.rd) / a.n
    if ctx > 150_000:
        out.append(f"Contexte moyen de {ctx/1000:.0f} k tokens par appel : /clear entre deux sujets, ou /compact, "
                   "réduit le coût de chaque appel suivant.")
    hit = a.rd / max(a.rd + a.wr + a.inp, 1)
    if a.n >= 200 and hit < 0.85:
        out.append(f"Le cache n'est lu que pour {hit:.0%} de l'entrée. Changer de modèle ou modifier le début de la "
                   "conversation en cours de session le vide et fait payer plein tarif.")
    return out


def payload(store, S, days, rate, budget, start):
    """Les chiffres de la période sous forme de dictionnaire (pour --json et pour la page web)."""
    a = S["all"]
    tot = a.cost or 1.0
    if days == 1:
        series = dict(unit="hour", points=[dict(label=f"{h} h", cost=S["hod"][h].cost * rate if h in S["hod"] else 0.0,
                                                calls=S["hod"][h].n if h in S["hod"] else 0) for h in range(24)])
    else:
        pts, d = [], start.date()
        while d <= dt.datetime.now().astimezone().date():
            g = S["day"].get(d)
            pts.append(dict(label=d.isoformat(), cost=g.cost * rate if g else 0.0, calls=g.n if g else 0))
            d += dt.timedelta(days=1)
        series = dict(unit="day", points=pts)
    ranked = lambda dct: sorted(dct.items(), key=lambda x: -x[1].cost)
    return dict(
        version=1, generated=dt.datetime.now().astimezone().isoformat(timespec="seconds"), days=days, rate=rate,
        budget=(budget * days if budget else None), prices_date=PRICES_DATE, disclaimer=DISCLAIMER,
        total=dict(cost=a.cost * rate, cost_usd=a.cost, tokens_m=a.tokens / 1e6, calls=a.n,
                   cache_read=a.rd / max(a.rd + a.wr + a.inp, 1), active_hours=len(S["hours"]),
                   cost_per_active_hour=(a.cost * rate / len(S["hours"])) if S["hours"] else 0.0),
        series=series,
        models=[dict(name=k, family=family(k), cost=g.cost * rate, share=g.cost / tot, calls=g.n) for k, g in ranked(S["model"])],
        sides=[dict(name=k, cost=g.cost * rate, share=g.cost / tot, calls=g.n) for k, g in ranked(S["side"])],
        projects=[dict(name=store.project_name(k), cost=g.cost * rate, share=g.cost / tot, calls=g.n)
                  for k, g in ranked(S["proj"])[:8]],
        sessions=[dict(project=store.project_name(p), id=s[:8], cost=g.cost * rate, calls=g.n)
                  for (p, s), g in ranked(S["sess"])[:5]],
        tips=tips(S, rate, budget, days),
        unpriced=dict(S["unpriced"]),
    )


def render(store, S, days, rate, budget):
    L = []
    a = S["all"]
    label = "aujourd'hui" if days == 1 else f"{days} derniers jours"
    L.append(f"Claude Code : coût en équivalent API, {label} (tarifs publics du {PRICES_DATE}, 1 $ = {rate} €)")
    L.append("=" * 78)
    if not a.n:
        L.append("Aucun appel trouvé sur la période.")
    else:
        L.append(f"TOTAL  {money(a.cost, rate):>10}   {a.tokens/1e6:,.1f} M tokens   {a.n:,} appels   "
                 f"cache lu : {a.rd/max(a.rd+a.wr+a.inp,1):.0%} de l'entrée".replace(",", " "))
        if days > 1:
            L.append("")
            L.append("Par jour")
            top = max(g.cost for g in S["day"].values())
            for d in sorted(S["day"]):
                g = S["day"][d]
                L.append(f"  {d:%a %d/%m}  {money(g.cost, rate):>9}  {'█' * max(1, round(24 * g.cost / top))}")
        L.append("")
        L.append("Par modèle")
        for k, g in sorted(S["model"].items(), key=lambda x: -x[1].cost):
            L.append(f"  {k:<26}{money(g.cost, rate):>10}  {g.cost/a.cost:>4.0%}  {g.n:>6,} appels".replace(",", " "))
        L.append("")
        L.append("Agent principal ou sous-agents")
        for k, g in sorted(S["side"].items(), key=lambda x: -x[1].cost):
            L.append(f"  {k:<26}{money(g.cost, rate):>10}  {g.cost/a.cost:>4.0%}  {g.n:>6,} appels".replace(",", " "))
        L.append("")
        L.append("Par projet")
        for k, g in sorted(S["proj"].items(), key=lambda x: -x[1].cost)[:6]:
            L.append(f"  {store.project_name(k)[:26]:<26}{money(g.cost, rate):>10}  {g.cost/a.cost:>4.0%}")
        L.append("")
        L.append("Sessions les plus coûteuses")
        for (p, s), g in sorted(S["sess"].items(), key=lambda x: -x[1].cost)[:3]:
            L.append(f"  {store.project_name(p)[:20]:<20} {s[:8]}  {money(g.cost, rate):>9}  {g.n:>6,} appels".replace(",", " "))
        t = tips(S, rate, budget, days)
        if t:
            L.append("")
            L.append("Pistes d'économie")
            L.extend("  • " + x for x in t)
    if S["unpriced"]:
        L.append("")
        L.append("⚠ Modèles sans tarif connu, non comptés : " +
                 ", ".join(f"{k} ({n} appels)" for k, n in S["unpriced"].items()) +
                 ". Ajoutez-les avec --prices (voir le README).")
    L.append("")
    L.append(DISCLAIMER)
    return "\n".join(L)


# --------------------------------------------------------------------------------------
# Tableau de bord web : un petit serveur local, joignable uniquement depuis ce poste.
# --------------------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Claude Code · coût</title>
<style>
:root{
  color-scheme:light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10); --wash:rgba(11,11,11,.05);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --other:#898781;
  --track:#cde2fb; --warn:#fab219; --crit:#d03b3b; --crit-ink:#b02a2a; --warn-ink:#8a5a00;
}
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10); --wash:rgba(255,255,255,.06);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --other:#898781;
    --track:#0d366b; --crit-ink:#ec6a6a; --warn-ink:#fab219;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10); --wash:rgba(255,255,255,.06);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --other:#898781;
  --track:#0d366b; --crit-ink:#ec6a6a; --warn-ink:#fab219;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:20px;margin:0 0 2px;font-weight:650}
h2{font-size:14px;margin:0 0 12px;font-weight:650}
.sub{color:var(--ink2);margin:0 0 16px}
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.seg-ctl{display:inline-flex;border:1px solid var(--ring);border-radius:8px;overflow:hidden;background:var(--surface)}
.seg-ctl button{font:inherit;color:var(--ink2);background:none;border:0;padding:7px 14px;cursor:pointer}
.seg-ctl button+button{border-left:1px solid var(--ring)}
.seg-ctl button:hover{background:var(--wash)}
.seg-ctl button[aria-pressed="true"]{color:var(--ink);font-weight:650;background:var(--wash)}
.seg-ctl button:focus-visible,.hit:focus-visible,.seg:focus-visible,.rrow:focus-visible,summary:focus-visible{outline:2px solid var(--s1);outline-offset:2px}
.updated{color:var(--muted);font-size:12px;margin-left:auto}
.banner{display:none;background:var(--surface);border:1px solid var(--crit);color:var(--crit-ink);border-radius:8px;padding:8px 12px;margin-bottom:12px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:18px 20px}
.grid{display:grid;gap:16px;margin-bottom:16px}
.g-top{grid-template-columns:minmax(0,1.15fr) minmax(0,1fr)}
.g-2{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
@media (max-width:760px){.g-top,.g-2{grid-template-columns:minmax(0,1fr)}}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:14px 16px}
.lab{color:var(--ink2);font-size:12px}
.val{font-size:24px;font-weight:650;margin-top:2px}
.hint{color:var(--muted);font-size:12px;margin-top:2px}
.hero .lab{font-size:13px}
.hero .big{font-size:56px;line-height:1.05;font-weight:650;margin:4px 0 8px}
.meter{height:8px;border-radius:4px;background:var(--track);overflow:hidden}
.meter>i{display:block;height:100%;border-radius:4px;background:var(--s1)}
.meter.warn>i{background:var(--warn)} .meter.crit>i{background:var(--crit)}
.state{display:flex;gap:6px;align-items:center;margin-top:8px;font-size:13px;color:var(--ink2)}
.state.warn{color:var(--warn-ink)} .state.crit{color:var(--crit-ink)}
.chart{width:100%}
.chart svg{display:block;max-width:100%}
.hit{fill:transparent;cursor:default;outline:none}
.bar{transition:opacity .12s}
.segbar{display:flex;gap:2px;height:28px}
.seg{border-radius:4px;display:flex;align-items:center;padding:0 8px;font-size:12px;font-weight:600;white-space:nowrap;min-width:0;outline:none}
.seg:hover{filter:brightness(1.1)}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:12px}
.legend span{display:inline-flex;align-items:center;gap:6px;color:var(--ink2)}
.legend b{color:var(--ink);font-weight:600}
.sw{width:10px;height:10px;border-radius:2px;display:inline-block}
.rrow{display:grid;grid-template-columns:minmax(80px,150px) 1fr 64px;gap:10px;align-items:center;padding:5px 0;outline:none}
.rrow .nm{color:var(--ink2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rrow .tr{height:14px}
.rrow .fl{height:14px;border-radius:0 4px 4px 0;background:var(--s1);transition:opacity .12s}
.rrow:hover .fl{opacity:.8}
.rrow .v{text-align:right;font-weight:600}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;color:var(--ink2);font-weight:500;border-bottom:1px solid var(--axis);padding:6px 8px 6px 0}
td{padding:6px 8px 6px 0;border-bottom:1px solid var(--grid)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
details{margin-top:10px}
summary{cursor:pointer;color:var(--ink2);font-size:12px;width:max-content}
details table{margin-top:8px}
ul.tips{margin:0;padding:0;list-style:none;display:grid;gap:8px}
ul.tips li{padding-left:16px;position:relative;color:var(--ink)}
ul.tips li::before{content:"";position:absolute;left:0;top:.6em;width:6px;height:6px;border-radius:50%;background:var(--s1)}
.empty{display:none;color:var(--ink2);padding:24px 0}
body[data-empty="true"] .needs-data{display:none}
body[data-empty="true"] .empty{display:block}
footer{color:var(--muted);font-size:12px;margin-top:8px}
.tip{position:fixed;z-index:10;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--ring);
  border-radius:8px;padding:8px 10px;box-shadow:0 4px 16px rgba(0,0,0,.18);font-size:12px;max-width:260px}
.tip .t{color:var(--ink2)} .tip .v{font-size:15px;font-weight:650;margin:2px 0;display:flex;align-items:center;gap:6px}
.tip .k{width:12px;height:3px;border-radius:2px;display:inline-block}
.loading main{opacity:.6;transition:opacity .2s}
</style>
</head>
<body data-empty="false">
<main>
  <h1>Claude Code · coût en équivalent API</h1>
  <p class="sub" id="sub">Chargement…</p>
  <div class="banner" id="banner" role="alert">Connexion au script perdue. Relancez-le puis rechargez la page.</div>
  <div class="filters">
    <div class="seg-ctl" role="group" aria-label="Période" id="periods"></div>
    <span class="updated" id="updated"></span>
  </div>
  <p class="empty">Aucun appel à Claude Code trouvé sur cette période.</p>
  <div class="grid g-top needs-data">
    <section class="card hero" aria-labelledby="hero-lab">
      <div class="lab" id="hero-lab">Coût sur la période</div>
      <div class="big" id="hero-val">–</div>
      <div id="hero-budget"></div>
    </section>
    <div class="tiles" id="tiles"></div>
  </div>
  <section class="card needs-data" style="margin-bottom:16px">
    <h2 id="ts-title">Coût heure par heure</h2>
    <div class="chart" id="ts"></div>
    <details><summary>Voir en tableau</summary><div id="ts-tbl"></div></details>
  </section>
  <div class="grid g-2 needs-data">
    <section class="card">
      <h2>Part de chaque modèle dans le coût</h2>
      <div id="models"></div>
      <details><summary>Voir en tableau</summary><div id="models-tbl"></div></details>
    </section>
    <section class="card">
      <h2>Agent principal et sous-agents</h2>
      <div id="sides"></div>
      <details><summary>Voir en tableau</summary><div id="sides-tbl"></div></details>
    </section>
  </div>
  <div class="grid g-2 needs-data">
    <section class="card">
      <h2>Coût par projet</h2>
      <div id="projects"></div>
    </section>
    <section class="card">
      <h2>Sessions les plus coûteuses</h2>
      <div id="sessions"></div>
    </section>
  </div>
  <section class="card needs-data" style="margin-bottom:16px">
    <h2>Pistes d'économie</h2>
    <div id="tips"></div>
  </section>
  <footer id="foot"></footer>
</main>
<script>
"use strict";
const $ = s => document.querySelector(s);
const PERIODS = [[1, "Aujourd'hui"], [7, "7 jours"], [30, "30 jours"]];
const FAMCOLOR = {opus: "var(--s1)", sonnet: "var(--s2)", haiku: "var(--s3)", fable: "var(--s4)", mythos: "var(--s4)", autre: "var(--other)"};
const FAMINK = {fable: "#0b0b0b", mythos: "#0b0b0b"};
const nf0 = new Intl.NumberFormat("fr-FR", {maximumFractionDigits: 0});
let days = 1, data = null;
const themeParam = new URLSearchParams(location.search).get("theme");   // ?theme=light ou ?theme=dark
if (themeParam === "light" || themeParam === "dark") document.documentElement.dataset.theme = themeParam;
try { days = +localStorage.getItem("ct-days") || 1; } catch (e) {}
const qDays = +new URLSearchParams(location.search).get("days");        // ?days=7
if (PERIODS.some(p => p[0] === qDays)) days = qDays;
if (!PERIODS.some(p => p[0] === days)) days = 1;

function eur(x) {
  const d = Math.abs(x) < 10 ? 1 : 0;
  return new Intl.NumberFormat("fr-FR", {style: "currency", currency: "EUR", minimumFractionDigits: d, maximumFractionDigits: d}).format(x);
}
const tok = m => m >= 1000 ? (m / 1000).toLocaleString("fr-FR", {maximumFractionDigits: 1}) + " Md" : nf0.format(m) + " M";
const pct = x => Math.round(x * 100) + " %";
function el(tag, attrs, parent, text) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (text != null) n.textContent = text;
  if (parent) parent.appendChild(n);
  return n;
}
function h(tag, cls, parent, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  if (parent) parent.appendChild(n);
  return n;
}

/* ---- infobulle : un seul élément, texte inséré avec textContent ---- */
const tip = h("div", "tip", document.body); tip.hidden = true;
function showTip(x, y, title, value, sub, color) {
  tip.textContent = "";
  h("div", "t", tip, title);
  const v = h("div", "v", tip);
  if (color) { const k = h("span", "k", v); k.style.background = color; }
  h("span", "", v, value);
  if (sub) h("div", "t", tip, sub);
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.max(8, Math.min(innerWidth - r.width - 8, x + 14)) + "px";
  tip.style.top = Math.max(8, Math.min(innerHeight - r.height - 8, y + 14)) + "px";
}
const hideTip = () => { tip.hidden = true; };
function wire(node, build) {
  node.addEventListener("pointermove", e => build(e.clientX, e.clientY, true));
  node.addEventListener("pointerleave", () => { hideTip(); build(0, 0, false); });
  node.addEventListener("focus", () => { const r = node.getBoundingClientRect(); build(r.left + r.width / 2, r.top, true); });
  node.addEventListener("blur", () => { hideTip(); build(0, 0, false); });
}

/* ---- axes : graduations arrondies ---- */
function niceTicks(max) {
  if (!(max > 0)) return [0, 1];
  const raw = max / 4, mag = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / mag;
  const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
  const out = [];
  for (let t = 0; t <= Math.ceil(max / step - 1e-9) * step + 1e-9; t += step) out.push(+t.toFixed(6));
  return out;
}

/* ---- colonnes : coût par heure ou par jour ---- */
function columns(host, pts, aria) {
  host.textContent = "";
  const W = Math.max(host.clientWidth, 300), H = 230, m = {l: 50, r: 8, t: 20, b: 26};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const ticks = niceTicks(Math.max(...pts.map(p => p.value), 0)), top = ticks[ticks.length - 1], step = ticks[1] - ticks[0];
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": aria}, host);
  for (const t of ticks) {
    const y = m.t + ih - ih * t / top;
    el("line", {x1: m.l, x2: W - m.r, y1: y, y2: y, stroke: t === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1, "shape-rendering": "crispEdges"}, svg);
    el("text", {x: m.l - 8, y: y + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": 11},
       svg, (step < 1 ? t.toLocaleString("fr-FR", {maximumFractionDigits: 1}) : nf0.format(t)) + " €");
  }
  const slot = iw / pts.length, bw = Math.min(24, Math.max(slot - 4, 2)), every = Math.ceil(pts.length / 8);
  const peak = pts.reduce((b, p, i) => p.value > pts[b].value ? i : b, 0);
  pts.forEach((p, i) => {
    const x = m.l + slot * i + (slot - bw) / 2, hgt = ih * p.value / top, y = m.t + ih - hgt;
    let bar = null;
    if (hgt > 0.5) {
      const r = Math.min(4, hgt, bw / 2);
      bar = el("path", {d: `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + bw - r}Q${x + bw},${y} ${x + bw},${y + r}V${y + hgt}Z`, fill: "var(--s1)", class: "bar"}, svg);
    }
    if (i % every === 0) el("text", {x: m.l + slot * i + slot / 2, y: H - 8, "text-anchor": "middle", fill: "var(--muted)", "font-size": 11}, svg, p.axis);
    if (i === peak && p.value > 0) {
      const lx = Math.max(m.l + 24, Math.min(W - m.r - 24, x + bw / 2));
      el("text", {x: lx, y: y - 6, "text-anchor": "middle", fill: "var(--ink2)", "font-size": 11, "font-weight": 600}, svg, eur(p.value));
    }
    const hit = el("rect", {x: m.l + slot * i, y: m.t, width: slot, height: ih, class: "hit", tabindex: 0, "aria-label": `${p.label} : ${eur(p.value)}, ${p.calls} appels`}, svg);
    wire(hit, (cx, cy, on) => {
      if (bar) bar.style.opacity = on ? 0.75 : 1;
      if (on) showTip(cx, cy, p.label, eur(p.value), p.calls + " appels", "var(--s1)");
    });
  });
}

/* ---- barre segmentée haute : un tout en quelques parts ---- */
function segBar(host, items, aria) {
  host.textContent = "";
  const bar = h("div", "segbar", host); bar.setAttribute("role", "img"); bar.setAttribute("aria-label", aria);
  const W = host.clientWidth || 400;
  for (const it of items) {
    if (!(it.share > 0)) continue;
    const s = h("div", "seg", bar); s.style.flex = `${it.share} 1 0`; s.style.background = it.color; s.tabIndex = 0;
    if (W * it.share >= 64) { s.textContent = pct(it.share); s.style.color = it.ink || "#fff"; }
    wire(s, (cx, cy, on) => { if (on) showTip(cx, cy, it.name, eur(it.cost), pct(it.share) + " du coût · " + it.calls + " appels", it.color); });
  }
  const lg = h("div", "legend", host);
  for (const it of items) {
    const sp = h("span", "", lg), sw = h("i", "sw", sp); sw.style.background = it.color;
    h("span", "", sp, it.name); h("b", "", sp, eur(it.cost));
  }
}

/* ---- jauge : une part contre un tout ---- */
function meter(host, frac, cls) {
  const m = h("div", "meter " + (cls || ""), host); m.setAttribute("role", "img");
  m.setAttribute("aria-label", pct(frac)); h("i", "", m).style.width = Math.max(0, Math.min(1, frac)) * 100 + "%";
}

/* ---- barres horizontales classées ---- */
function ranked(host, rows) {
  host.textContent = "";
  const max = Math.max(...rows.map(r => r.cost), 0.0001);
  for (const r of rows) {
    const row = h("div", "rrow", host); row.tabIndex = 0;
    h("div", "nm", row, r.name).title = r.name;
    const tr = h("div", "tr", row); h("div", "fl", tr).style.width = Math.max(r.cost / max * 100, 1) + "%";
    h("div", "v", row, eur(r.cost));
    wire(row, (cx, cy, on) => { if (on) showTip(cx, cy, r.name, eur(r.cost), pct(r.share) + " du coût · " + r.calls + " appels", "var(--s1)"); });
  }
}

function table(host, head, rows) {
  host.textContent = "";
  const t = h("table", "", host), tr = h("tr", "", h("thead", "", t));
  head.forEach((c, i) => h("th", i ? "n" : "", tr, c));
  const tb = h("tbody", "", t);
  for (const r of rows) { const x = h("tr", "", tb); r.forEach((c, i) => h("td", i ? "n" : "", x, c)); }
}

function dayLabel(iso) {
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString("fr-FR", {weekday: "short", day: "numeric", month: "short"});
}

function render() {
  const d = data, T = d.total;
  document.body.dataset.empty = T.calls === 0 ? "true" : "false";
  $("#sub").textContent = `${days === 1 ? "Aujourd'hui" : "Les " + days + " derniers jours"} · tarifs publics du ${d.prices_date} · 1 $ = ${String(d.rate).replace(".", ",")} €`;
  $("#updated").textContent = "Mis à jour à " + new Date().toLocaleTimeString("fr-FR");
  $("#hero-lab").textContent = "Coût " + (days === 1 ? "aujourd'hui" : "sur " + days + " jours");
  $("#hero-val").textContent = eur(T.cost);
  const hb = $("#hero-budget"); hb.textContent = "";
  if (d.budget) {
    const f = T.cost / d.budget, cls = f > 1 ? "crit" : f >= 0.8 ? "warn" : "";
    meter(hb, f, cls);
    const st = h("div", "state " + cls, hb);
    if (cls) { const ic = el("svg", {width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true"}, st);
      el("path", {d: "M7 1.5 13 12.5H1Z", fill: "none", stroke: "currentColor", "stroke-width": 1.5, "stroke-linejoin": "round"}, ic);
      el("path", {d: "M7 5.5v3.2M7 10.4v.1", stroke: "currentColor", "stroke-width": 1.5, "stroke-linecap": "round"}, ic); }
    h("span", "", st, (f > 1 ? "Budget dépassé : " : f >= 0.8 ? "Proche du budget : " : "") + pct(f) + " des " + eur(d.budget) + " prévus");
  } else {
    h("div", "hint", hb, "Astuce : lancez le script avec --budget 50 pour suivre un budget par jour.");
  }
  const tl = $("#tiles"); tl.textContent = "";
  for (const [l, v, s] of [
    ["Tokens", tok(T.tokens_m), "lus, écrits et mis en cache"],
    ["Appels au modèle", nf0.format(T.calls), "sous-agents compris"],
    ["Cache relu", pct(T.cache_read), "de l'entrée, à prix réduit"],
    ["Coût par heure active", eur(T.cost_per_active_hour), T.active_hours + " h d'activité"]]) {
    const t = h("div", "tile", tl); h("div", "lab", t, l); h("div", "val", t, v); h("div", "hint", t, s);
  }
  $("#ts-title").textContent = d.series.unit === "hour" ? "Coût heure par heure" : "Coût jour par jour";
  const isHour = d.series.unit === "hour";
  const pts = d.series.points.map(p => ({
    label: isHour ? "À " + p.label : dayLabel(p.label),
    axis: isHour ? p.label : String(new Date(p.label + "T12:00:00").getDate()),
    value: p.cost, calls: p.calls}));
  const drawTs = () => columns($("#ts"), pts, $("#ts-title").textContent);
  drawTs();
  table($("#ts-tbl"), [d.series.unit === "hour" ? "Heure" : "Jour", "Coût", "Appels"], pts.map(p => [p.label, eur(p.value), nf0.format(p.calls)]));

  const mitems = d.models.map(m => ({name: m.name, cost: m.cost, share: m.share, calls: m.calls, color: FAMCOLOR[m.family], ink: FAMINK[m.family]}));
  const drawModels = () => segBar($("#models"), mitems, "Part de chaque modèle dans le coût");
  drawModels();
  table($("#models-tbl"), ["Modèle", "Coût", "Part", "Appels"], d.models.map(m => [m.name, eur(m.cost), pct(m.share), nf0.format(m.calls)]));

  const sd = $("#sides"); sd.textContent = "";
  const sub = d.sides.find(s => s.name === "sous-agents"), subShare = sub ? sub.share : 0;
  h("div", "lab", sd, "Part des sous-agents dans le coût"); h("div", "val", sd, pct(subShare));
  h("div", "", sd).style.height = "8px"; meter(sd, subShare);
  h("div", "hint", sd, sub ? eur(sub.cost) + " pour " + nf0.format(sub.calls) + " appels. Les sous-agents sont les assistants que Claude lance en parallèle." : "Aucun sous-agent sur la période.");
  table($("#sides-tbl"), ["Type", "Coût", "Part", "Appels"], d.sides.map(s => [s.name, eur(s.cost), pct(s.share), nf0.format(s.calls)]));

  ranked($("#projects"), d.projects.slice(0, 6));
  table($("#sessions"), ["Projet", "Session", "Coût", "Appels"], d.sessions.map(s => [s.project, s.id, eur(s.cost), nf0.format(s.calls)]));
  const tp = $("#tips"); tp.textContent = "";
  if (d.tips.length) { const ul = h("ul", "tips", tp); d.tips.forEach(t => h("li", "", ul, t.replace(/`/g, ""))); }
  else h("div", "hint", tp, "Rien à signaler sur cette période.");
  $("#foot").textContent = d.disclaimer + (Object.keys(d.unpriced).length ? " Modèles sans tarif connu, non comptés : " + Object.keys(d.unpriced).join(", ") + "." : "");
  render.redraw = () => { drawTs(); drawModels(); };
}

function periods() {
  const box = $("#periods"); box.textContent = "";
  for (const [n, name] of PERIODS) {
    const b = h("button", "", box, name); b.type = "button"; b.setAttribute("aria-pressed", String(n === days));
    b.addEventListener("click", () => { days = n; try { localStorage.setItem("ct-days", n); } catch (e) {} periods(); load(); });
  }
}
async function load() {
  document.body.classList.add("loading");
  try {
    const r = await fetch("/api/data?days=" + days, {cache: "no-store"});
    if (!r.ok) throw new Error(r.status);
    data = await r.json(); render(); $("#banner").style.display = "none";
  } catch (e) { $("#banner").style.display = "block"; }
  document.body.classList.remove("loading");
}
let raf = 0;
addEventListener("resize", () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => data && render.redraw && render.redraw()); });
periods(); load();
setInterval(() => { if (!document.hidden) load(); }, 30000);
</script>
</body>
</html>
"""


def make_server(store, prices, rate, budget, port):
    """Serveur local : n'écoute que 127.0.0.1 et refuse toute requête dont l'en-tête Host n'est pas local."""
    lock = threading.Lock()
    html = HTML.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost", "::1"):
                return self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path == "/api/data":
                try:
                    days = int(parse_qs(u.query).get("days", ["1"])[0])
                except ValueError:
                    days = 1
                days = max(1, min(days, WEB_MAX_DAYS))
                with lock:
                    store.refresh()
                    start, end = window(days)
                    body = json.dumps(payload(store, summarize(store, start, end, prices), days, rate, budget, start),
                                      ensure_ascii=False).encode("utf-8")
                return self._send(200, body, "application/json; charset=utf-8")
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    ThreadingHTTPServer.daemon_threads = True
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:                       # port déjà pris : on laisse le système en choisir un libre
        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def main():
    setup_console()
    ap = argparse.ArgumentParser(description="Coût de votre usage de Claude Code, en équivalent API.")
    ap.add_argument("--days", type=int, default=1, help="nombre de jours à afficher, aujourd'hui compris (défaut 1)")
    ap.add_argument("--watch", action="store_true", help="suivi en direct dans le terminal, rafraîchi toutes les 30 secondes")
    ap.add_argument("--web", action="store_true", help="tableau de bord dans le navigateur (serveur local)")
    ap.add_argument("--port", type=int, default=8765, help="port du tableau de bord web (défaut 8765)")
    ap.add_argument("--no-browser", action="store_true", help="avec --web : ne pas ouvrir le navigateur")
    ap.add_argument("--json", action="store_true", help="affiche les chiffres au format JSON, pour un autre outil")
    ap.add_argument("--budget", type=float, help="budget par jour en € : alerte si dépassé")
    ap.add_argument("--eur-rate", type=float, default=0.86, help="taux 1 $ en € (défaut 0,86, indicatif)")
    ap.add_argument("--prices", help="fichier JSON de tarifs : {\"modele\": [entrée, sortie, écriture 5 min, écriture 1 h, lecture]}")
    ap.add_argument("--root", default=str(Path.home() / ".claude" / "projects"), help="dossier des journaux Claude Code")
    args = ap.parse_args()

    prices = dict(PRICES)
    if args.prices:
        with open(args.prices, encoding="utf-8") as fh:
            prices.update({k: tuple(v) for k, v in json.load(fh).items()})
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"Dossier introuvable : {root}. Claude Code a-t-il déjà été utilisé sur ce poste ?")
    days = max(args.days, 1)

    if args.web:
        store = Store(root, window(WEB_MAX_DAYS)[0])
        print("Lecture des journaux…", flush=True)
        store.refresh()
        server = make_server(store, prices, args.eur_rate, args.budget, args.port)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        print(f"Tableau de bord : {url}   (Ctrl+C pour arrêter)", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return

    store = Store(root, window(days)[0])
    while True:
        store.refresh()
        start, end = window(days)
        S = summarize(store, start, end, prices)
        if args.json:
            print(json.dumps(payload(store, S, days, args.eur_rate, args.budget, start), ensure_ascii=False, indent=2))
            return
        text = render(store, S, days, args.eur_rate, args.budget)
        if args.watch:
            print("\033[2J\033[H" + text + f"\n\nMis à jour à {dt.datetime.now():%H:%M:%S}. Ctrl+C pour quitter.", flush=True)
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                return
            store.since = window(days)[0]
        else:
            print(text)
            return


if __name__ == "__main__":
    main()
