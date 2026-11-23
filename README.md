# claude-tokenomie

Combien coûte votre usage de Claude Code ? Un seul fichier Python, sans installation, qui lit vos journaux locaux et affiche le coût en équivalent API, par jour, par modèle, par projet et pour les sous-agents.

```
python3 claude_tokenomie.py              # aujourd'hui
python3 claude_tokenomie.py --days 7     # 7 derniers jours, jour par jour
python3 claude_tokenomie.py --watch      # suivi en direct, rafraîchi toutes les 30 s
python3 claude_tokenomie.py --watch --budget 50    # alerte au-delà de 50 € par jour
```

Il fonctionne avec le terminal et avec l'extension VS Code : les deux écrivent les mêmes journaux. Lancez-le dans un terminal à côté de votre éditeur.

## Ce que vous obtenez

- Le coût total et le nombre de tokens de la période.
- Le détail par jour, par modèle (Opus, Sonnet, Haiku), par projet, et la part des sous-agents.
- Vos sessions les plus coûteuses.
- Des pistes d'économie quand elles s'appliquent : trop d'Opus pour des tâches simples, sous-agents coûteux, contexte très long, cache mal utilisé.

## Confidentialité

- Tout se passe sur votre poste. Le script n'utilise pas le réseau.
- Il ne lit que les compteurs de tokens et le nom du modèle. Le contenu de vos conversations n'est jamais lu ni affiché.
- Rien n'est écrit sur le disque. Vos chiffres ne sont partagés avec personne.

## À savoir

- **C'est un équivalent API, pas une facture.** Les sièges Claude Enterprise sont au forfait. Le chiffre sert de repère d'efficacité : il montre ce que coûterait le même usage via l'API.
- **Il ne couvre que Claude Code sur ce poste.** Le chat de claude.ai et vos autres machines n'y sont pas.
- **Les tarifs sont ceux du 21/11/2026**, tirés de la page publique d'Anthropic. Ils sont définis en tête du script (`PRICES`). Le taux 1 $ = 0,86 € est indicatif (`--eur-rate`).
- **Les journaux ne sont pas un format garanti par Anthropic.** Une mise à jour de Claude Code peut changer leur structure. En cas de chiffres bizarres, comparez avec `/usage` dans Claude Code.
- **Le cache** : environ 98 % des tokens sont des relectures du contexte, facturées à 5 % du prix normal. C'est normal, et c'est ce qui rend Claude Code abordable.

## Modèle absent de la liste

Un modèle sans tarif connu est signalé et n'est pas compté. Ajoutez-le dans un fichier JSON (tarifs en $ par million de tokens : entrée, sortie, écriture cache 5 min, écriture cache 1 h, lecture cache) :

```json
{ "opus-6": [5, 25, 6.25, 10, 0.5] }
```

puis `python3 claude_tokenomie.py --prices mes_tarifs.json`.

## Prérequis

Python 3.9 ou plus récent. Aucune bibliothèque à installer. Testé sur macOS ; les chemins sont gérés avec `pathlib` pour Windows, mais ce n'est pas encore testé.

## Pour aller plus loin

- Dans Claude Code, `/usage` affiche la consommation de votre forfait.
- Pour suivre toute l'organisation, la télémétrie OpenTelemetry de Claude Code peut être activée par l'équipe IT via les réglages gérés. Cela demande un outil de collecte et un accord sur l'usage des données nominatives.
