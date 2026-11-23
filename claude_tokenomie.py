#!/usr/bin/env python3
"""claude_tokenomie : le coût de votre usage de Claude Code, en équivalent API.

Lit les journaux locaux de Claude Code (~/.claude/projects), additionne les tokens
et les valorise au tarif public de l'API Anthropic. Fonctionne avec le terminal et
avec l'extension VS Code, qui écrivent les mêmes journaux.

- Aucune dépendance : Python 3.9 ou plus récent suffit.
- Rien ne sort de votre poste : pas de réseau, et le contenu de vos conversations n'est jamais lu.
- Le coût affiché est un équivalent API au tarif public. Ce n'est pas une facture :
  les sièges Claude Enterprise sont au forfait.

Exemples :
    python3 claude_tokenomie.py                 # aujourd'hui
    python3 claude_tokenomie.py --days 7        # 7 derniers jours, jour par jour
    python3 claude_tokenomie.py --watch         # suivi en direct (rafraîchi toutes les 30 s)
    python3 claude_tokenomie.py --watch --budget 50   # alerte au-delà de 50 €/jour
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

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
    """Lit les journaux en incrémental : en mode --watch, seuls les nouveaux octets sont relus."""

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
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return
        cwd = d.get("cwd")
        if cwd and (proj not in self.cwd or len(cwd) < len(self.cwd[proj])):
            self.cwd[proj] = cwd
        if m.get("model", "").startswith("<"):   # message interne (<synthetic>) : aucun appel facturé
            return
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


def summarize(store, start, end, prices):
    S = dict(all=Agg(), day=defaultdict(Agg), model=defaultdict(Agg), proj=defaultdict(Agg),
             side=defaultdict(Agg), sess=defaultdict(Agg), unpriced=defaultdict(int))
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
    if budget and days == 1 and a.cost * rate > budget:
        out.append(f"Budget du jour dépassé : {a.cost*rate:.0f} € pour {budget:.0f} € prévus.")
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


def render(store, S, start, end, days, prices, rate, budget):
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
    L.append("Équivalent API au tarif public, hors chat claude.ai. Ce n'est pas une facture (sièges au forfait).")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Coût de votre usage de Claude Code, en équivalent API.")
    ap.add_argument("--days", type=int, default=1, help="nombre de jours à afficher, aujourd'hui compris (défaut 1)")
    ap.add_argument("--watch", action="store_true", help="suivi en direct, rafraîchi toutes les 30 secondes")
    ap.add_argument("--budget", type=float, help="alerte si le coût du jour dépasse ce montant en €")
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

    midnight = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - dt.timedelta(days=max(args.days, 1) - 1)
    store = Store(root, start)
    while True:
        store.refresh()
        end = dt.datetime.now().astimezone() + dt.timedelta(seconds=1)
        S = summarize(store, start, end, prices)
        text = render(store, S, start, end, max(args.days, 1), prices, args.eur_rate, args.budget)
        if args.watch:
            print("\033[2J\033[H" + text + f"\n\nMis à jour à {dt.datetime.now():%H:%M:%S}. Ctrl+C pour quitter.", flush=True)
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                return
            midnight = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            start = midnight - dt.timedelta(days=max(args.days, 1) - 1)
        else:
            print(text)
            return


if __name__ == "__main__":
    main()
