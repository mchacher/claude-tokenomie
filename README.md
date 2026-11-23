# claude-tokenomie

Combien coûte votre usage de Claude Code ? Un seul fichier Python, sans installation, qui lit vos journaux locaux et affiche le coût en équivalent API : par jour, par modèle, par projet, avec la part des sous-agents et des pistes d'économie.

Il fonctionne sous **Windows, macOS et Linux**, dans le terminal comme avec l'extension VS Code : les deux écrivent les mêmes journaux.

## Lancer

Sous macOS et Linux, tapez `python3`. Sous Windows, tapez `py` (ou `python`).

```
python3 claude_tokenomie.py --web        # tableau de bord dans le navigateur
python3 claude_tokenomie.py              # aujourd'hui, dans le terminal
python3 claude_tokenomie.py --days 7     # 7 derniers jours, jour par jour
python3 claude_tokenomie.py --watch      # suivi en direct dans le terminal (toutes les 30 s)
python3 claude_tokenomie.py --json       # les mêmes chiffres, pour un autre outil
```

Ajoutez `--budget 50` pour suivre un budget de 50 € par jour : le tableau de bord affiche une jauge qui passe à l'orange puis au rouge.

## Le tableau de bord (`--web`)

Une page qui s'ouvre dans le navigateur et se rafraîchit toute seule :

- le coût du jour (ou des 7 ou 30 derniers jours) en grand, avec la jauge de budget ;
- le coût heure par heure ou jour par jour, avec une infobulle au survol ;
- la part de chaque modèle (Opus, Sonnet, Haiku) et celle des sous-agents ;
- le coût par projet et les sessions les plus coûteuses ;
- des pistes d'économie, et une vue en tableau sous chaque graphique.

Elle suit le mode clair ou sombre de votre système. Dans VS Code, ouvrez l'adresse affichée avec « Simple Browser » (palette de commandes : *Simple Browser: Show*) pour l'avoir à côté du code. Le mode sombre se force avec `?theme=dark`.

## Confidentialité

- Tout se passe sur votre poste. Le tableau de bord est servi uniquement à votre propre machine (`127.0.0.1`), jamais au réseau, et refuse les requêtes qui ne viennent pas de là.
- Le script n'accède à aucun service externe. La page dessine ses graphiques elle-même : aucune ressource n'est chargée depuis Internet.
- Il ne lit que les compteurs de tokens et le nom du modèle. Le contenu de vos conversations n'est jamais lu ni affiché.
- Rien n'est écrit sur le disque. Vos chiffres ne sont partagés avec personne.

## À savoir

- **C'est un équivalent API, pas une facture.** Les sièges Claude Enterprise sont au forfait. Le chiffre sert de repère d'efficacité : il montre ce que coûterait le même usage via l'API.
- **Il ne couvre que Claude Code sur ce poste.** Le chat de claude.ai et vos autres machines n'y sont pas.
- **Les tarifs sont ceux du 21/11/2026**, tirés de la page publique d'Anthropic. Ils sont définis en tête du script (`PRICES`). Le taux 1 $ = 0,86 € est indicatif (`--eur-rate`).
- **Les journaux ne sont pas un format garanti par Anthropic.** Une mise à jour de Claude Code peut changer leur structure. En cas de chiffres bizarres, comparez avec `/usage` dans Claude Code.
- **Le cache** : environ 98 % des tokens sont des relectures du contexte, facturées à 5 % du prix normal. C'est normal, et c'est ce qui rend Claude Code abordable.

## Windows

- Utilisez de préférence **Windows Terminal** (installé par défaut sous Windows 11). L'ancienne console `cmd.exe` peut mal afficher les barres du mode terminal ; le tableau de bord web n'a pas ce défaut.
- Python se trouve sur le Microsoft Store ou sur python.org. La version 3.9 ou plus récente suffit.
- Les journaux sont lus dans `%USERPROFILE%\.claude\projects`. Si les vôtres sont ailleurs, indiquez-le avec `--root`.
- Le script est vérifié automatiquement sur Windows, macOS et Linux à chaque modification (onglet *Actions* du dépôt).

## Modèle absent de la liste

Un modèle sans tarif connu est signalé et n'est pas compté. Ajoutez-le dans un fichier JSON (tarifs en $ par million de tokens : entrée, sortie, écriture cache 5 min, écriture cache 1 h, lecture cache) :

```json
{ "opus-6": [5, 25, 6.25, 10, 0.5] }
```

puis `python3 claude_tokenomie.py --prices mes_tarifs.json`.

## Développer

```
python3 -m unittest discover -s tests -v
```

Les tests utilisent de faux journaux, sans aucune donnée réelle. Ils vérifient les calculs de coût, le dédoublonnage des messages, la lecture des chemins Windows, l'affichage sur une console Windows classique et la sécurité du serveur local.

## Pour aller plus loin

- Dans Claude Code, `/usage` affiche la consommation de votre forfait.
- `--json` sert de base à d'autres outils, par exemple un compteur dans la barre d'état de VS Code.
- Pour suivre toute l'organisation, la télémétrie OpenTelemetry de Claude Code peut être activée par l'équipe IT via les réglages gérés. Cela demande un outil de collecte et un accord sur l'usage des données nominatives.
