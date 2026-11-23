"""Tests de claude_tokenomie, avec de faux journaux : aucune donnée réelle.

Ils tournent sous Windows, macOS et Linux (voir .github/workflows/test.yml).
Lancer : python -m unittest discover -s tests -v   (sous Windows : py -m unittest discover -s tests -v)
"""
import datetime as dt
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "claude_tokenomie.py"
spec = importlib.util.spec_from_file_location("ct", SCRIPT)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

# Coûts attendus, calculés à la main au tarif public ($ par million de tokens)
COST_OPUS = (10 * 4 + 1000 * 20 + 100_000 * 8 + 2_000_000 * 0.20) / 1e6           # écriture cache 1 h
COST_SONNET = (1000 * 2 + 500 * 10 + 50_000 * 2.5 + 1_000_000 * 0.10) / 1e6        # écriture cache 5 min
COST_HAIKU_LONG = (150_000 * 0.50 + 1000 * 2.50) / 1e6                             # requête de plus de 100 000 tokens
TOTAL = COST_OPUS + COST_SONNET + COST_HAIKU_LONG


def line(mid, model, cwd, **usage):
    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({"type": "assistant", "timestamp": ts, "cwd": cwd,
                       "message": {"id": mid, "model": model, "usage": usage}})


def write(path, lines, newline="\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for x in lines:
            fh.write(x + newline)


def build_fixture(root):
    opus = line("m1", "claude-opus-5-5", "/home/dev/proj-a-folder", input_tokens=10, output_tokens=1000,
                cache_read_input_tokens=2_000_000, cache_creation_input_tokens=100_000,
                cache_creation={"ephemeral_1h_input_tokens": 100_000, "ephemeral_5m_input_tokens": 0})
    # Le même message écrit deux fois (une ligne par bloc de contenu) : il ne doit être compté qu'une fois.
    # Fins de ligne Windows (CRLF) pour ce fichier.
    write(root / "-proj-a" / "s1.jsonl", [opus, opus], newline="\r\n")
    write(root / "-proj-a" / "s1" / "subagents" / "agent-1.jsonl", [
        line("m2", "claude-sonnet-5-5", "/home/dev/proj-a-folder", input_tokens=1000, output_tokens=500,
             cache_read_input_tokens=1_000_000, cache_creation_input_tokens=50_000,
             cache_creation={"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 50_000})])
    win = r"C:\Users\dev\mon-projet"
    write(root / "-proj-b" / "s2.jsonl", [
        line("m3", "claude-haiku-5-5", win, input_tokens=150_000, output_tokens=1000),
        line("m4", "claude-foo-9", win, input_tokens=1000, output_tokens=1000),
        line("m5", "<synthetic>", win, input_tokens=0, output_tokens=0)])


class Tokenomie(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "projects"
        build_fixture(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def summary(self, days=2, prices=None):
        start, end = ct.window(days)
        store = ct.Store(self.root, start)
        store.refresh()
        return store, ct.summarize(store, start, end, prices or ct.PRICES)

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="cp1252")   # console Windows classique : doit rester sans plantage
        p = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), *args],
                           capture_output=True, env=env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        return p.stdout.decode("utf-8")

    def test_costs_match_public_prices(self):
        _, S = self.summary()
        self.assertAlmostEqual(S["all"].cost, TOTAL, places=6)

    def test_duplicate_messages_counted_once(self):
        _, S = self.summary()
        self.assertEqual(S["all"].n, 3)                      # m1, m2, m3 (m4 sans tarif, m5 interne)
        self.assertAlmostEqual(S["model"]["claude-opus-5-5"].cost, COST_OPUS, places=6)

    def test_subagents_are_detected(self):
        _, S = self.summary()
        self.assertEqual(S["side"]["sous-agents"].n, 1)
        self.assertEqual(S["side"]["agent principal"].n, 2)

    def test_unknown_model_is_reported_not_counted(self):
        _, S = self.summary()
        self.assertEqual(dict(S["unpriced"]), {"claude-foo-9": 1})     # "<synthetic>" est ignoré en silence

    def test_project_names_from_posix_and_windows_paths(self):
        store, _ = self.summary()
        self.assertEqual(store.project_name("-proj-a"), "proj-a-folder")
        self.assertEqual(store.project_name("-proj-b"), "mon-projet")

    def test_json_output_with_legacy_console_encoding(self):
        d = json.loads(self.run_cli("--json", "--days", "2"))
        self.assertEqual(d["total"]["calls"], 3)
        self.assertAlmostEqual(d["total"]["cost_usd"], TOTAL, places=6)
        self.assertEqual(len(d["series"]["points"]), 2)

    def test_text_output_with_legacy_console_encoding(self):
        out = self.run_cli("--days", "2")
        self.assertIn("Claude Code", out)
        self.assertIn("█", out)                                         # caractères non ASCII bien affichés
        self.assertIn("Modèles sans tarif connu", out)

    def test_prices_file_adds_a_model(self):
        f = Path(self.tmp.name) / "prices.json"
        f.write_text(json.dumps({"claude-foo-9": [1, 1, 1, 1, 1]}), encoding="utf-8")
        d = json.loads(self.run_cli("--json", "--days", "2", "--prices", str(f)))
        self.assertEqual(d["unpriced"], {})
        self.assertAlmostEqual(d["total"]["cost_usd"], TOTAL + 2000 / 1e6, places=6)

    def test_web_server(self):
        start, _ = ct.window(2)
        store = ct.Store(self.root, start)
        store.refresh()
        server = ct.make_server(store, ct.PRICES, 0.86, 100.0, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertEqual(server.server_address[0], "127.0.0.1")     # jamais exposé au réseau

            def get(path, host=None):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                c.request("GET", path, headers={"Host": host} if host else {})
                r = c.getresponse()
                body = r.read()
                c.close()
                return r.status, body

            code, body = get("/")
            self.assertEqual(code, 200)
            self.assertIn("<title>Claude Code", body.decode("utf-8"))
            code, body = get("/api/data?days=2")
            self.assertEqual(code, 200)
            d = json.loads(body.decode("utf-8"))
            self.assertEqual(d["total"]["calls"], 3)
            self.assertEqual(d["budget"], 200.0)                        # 100 € par jour sur 2 jours
            self.assertEqual(get("/api/data", host="evil.example")[0], 403)   # protection contre le détournement de DNS
            self.assertEqual(get("/inconnu")[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
