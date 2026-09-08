# VINTEDBOT

Mini-app Telegram personnelle, en français : recherches Vinted, notifications et suivi achats/ventes. Python 3.12+, SQLite, interface HTML/CSS/JS sans dépendance applicative Python. Hébergement prévu sur un serveur Linux avec Docker Compose et HTTPS (Caddy).

## État réel

- Implémenté : filtres CRUD, import des critères d’une URL Vinted France, pause/reprise, exclusions de mots, détection/dédoublonnage, file Telegram persistante avec retries, authentification Mini App, stock/ventes/frais/remboursements, export CSV, configuration Docker et tests.
- **Pas encore mis en service** : aucun token Telegram, identifiant utilisateur ni serveur n’est fourni dans ce dépôt. Aucun message réel n’a été envoyé pendant le développement.
- **Collecteur expérimental et désactivé par défaut** : l’URL publique `/api/v2/catalog/items` est non documentée officiellement, peut refuser l’accès sans session ou changer de format. Les tests utilisent des réponses simulées ; ils ne démontrent pas un accès fonctionnel au catalogue réel. Aucun cookie de compte, proxy rotatif, résolution de CAPTCHA ou changement d’identité n’est implémenté.
- Cette version est un outil de **suivi de gestion**, pas un logiciel de comptabilité certifié ni une synchronisation automatique des commandes Vinted. Conservez les justificatifs séparément. Pas de TVA, cotisations, déclaration fiscale, rapprochement bancaire ni achats automatiques.

## Installation

**VPS sans domaine personnel :** suivre [le démarrage avec tunnel HTTPS temporaire](docs/QUICKSTART_VPS.md). Le fichier `compose.tunnel.yaml` est une alternative au fichier standard ci-dessous, destinée aux premiers essais. Pas besoin d'acheter un domaine pour cette option.

1. Créer un bot auprès de [@BotFather](https://t.me/BotFather) avec `/newbot`. Garder son token secret.
2. Récupérer votre identifiant **numérique** Telegram, puis l’inscrire dans `TELEGRAM_ALLOWED_USER_IDS`. Il est disponible dans le champ `message.from.id` de `getUpdates` après un message à votre bot (voir le guide ci-dessous). Le pseudo `@...` ne suffit pas.
3. Préparer un serveur Linux avec Docker/Compose, un nom de domaine pointant vers ce serveur et les ports 80/443 accessibles.
4. Cloner le dépôt, puis copier `.env.example` vers `.env` et renseigner le token, l’ID et le domaine. `PUBLIC_URL` doit être l’origine HTTPS exacte, sans chemin. Ne pas publier `.env`.

```sh
git clone https://github.com/Torkor29/VINTEDBOT.git
cd VINTEDBOT
cp .env.example .env
# Modifier .env localement avec votre éditeur.
docker compose up -d --build
docker compose ps
```

5. Dans BotFather, configurer le bouton de menu / la Mini App du bot avec votre `PUBLIC_URL`. Envoyer `/start` **dans une conversation privée** avec le bot, puis ouvrir « Mon espace ». Le bot installe aussi un bouton de menu pour cette conversation. `/stop` suspend les notifications ; `/start` les réactive.
6. Ajouter une recherche : sur Vinted France, sélectionner les critères puis copier le lien `https://www.vinted.fr/catalog?...`. Le bot conserve mots-clés, catégories, marques, tailles, états, couleurs, matières et prix reconnus. Il refuse les paramètres inconnus afin de ne pas les supprimer silencieusement. Le tri est forcé sur les plus récents.

L’interface reste verrouillée hors Telegram. Il n’existe pas de mot de passe ou d’authentification de démonstration. L’authentification Telegram expire après une heure : fermer puis rouvrir la mini-app.

### Collecte et vitesse

Consultez les règles de Vinted et obtenez l’autorisation appropriée avant d’activer `VINTED_ACCESS_AUTHORIZED=true` et `VINTED_COLLECTION_ENABLED=true`. Ces options constituent une configuration opérateur, pas une autorisation accordée par Vinted. Les règles d’accès peuvent évoluer. La [page française consultée](https://www.vinted.fr/terms-and-conditions) le 8 septembre 2026 affiche une version annoncée pour le 5 octobre 2026, qui interdit les bots/scraping sauf autorisation ; la page de l’ancienne URL n’exposait pas son texte. Il faut donc confirmer la version applicable lors de l’activation. [robots.txt](https://www.vinted.fr/robots.txt) n’est pas une autorisation contractuelle.

- Minimum et défaut **15 s par nouveau filtre** ; choix 15 s, 30 s, 1 min ou plus dans la mini-app. Les filtres existants conservent leur intervalle : les modifier pour passer à 15 s. La mini-app recharge également ses données toutes les 15 s quand elle est visible, indépendamment du collecteur.
- Espacement global configurable, minimum et défaut **1 s entre débuts de requêtes**, y compris `robots.txt`. Ce n'est pas une limite déclarée ou approuvée par Vinted. Les filtres sont traités par ordre d’échéance, sans exécutions parallèles du collecteur ni rafale de rattrapage. Une installation antérieure doit modifier `GLOBAL_REQUEST_GAP_SECONDS=60` dans son `.env` pour bénéficier du nouveau réglage.
- Cadence effective ≈ au moins `max(intervalle du filtre, nombre de filtres actifs × limite globale)` en régime régulier, plus temps réseau et éventuels ralentissements. Le délai aléatoire de 0–15 s sur les passages normaux a été supprimé ; un aléa reste appliqué aux attentes après erreur. À titre d'exemple, 3 filtres à 15 s avec des réponses rapides peuvent être décalés et revérifiés chacun toutes les 15 s ; 20 filtres avec 1 s d'espacement ne peuvent pas tous l'être toutes les 15 s. Aucune promesse de temps réel ou de livraison dans un délai fixe.
- Chaque réponse déclenche immédiatement l’insertion dans la file persistante ; le worker Telegram la traite environ chaque seconde sous réserve de ses limites.
- Première exécution : mémorisation sans alerte. Modifier les critères ou reprendre un filtre crée une nouvelle référence ; les articles déjà présents à la reprise ne sont pas envoyés.
- Première page uniquement (96 résultats demandés). Une recherche trop large peut manquer des annonces publiées entre deux passages : affiner les critères. Aucun rattrapage exhaustif ou historique complet n’est garanti.
- Un même article ne produit qu’une alerte par utilisateur, même s’il correspond à plusieurs filtres. Une baisse de prix ne déclenche pas une seconde alerte.
- HTTP 401 sur le catalogue : le jeton anonyme est considéré comme périmé. La session est jetée et recréée, au maximum **3 fois par heure**, avec attente exponentielle entre les tentatives ; au-delà, arrêt persistant. Un cookie expiré est également détecté avant l’appel et remplacé sans erreur. HTTP 403, redirection, format inattendu ou refus robots : arrêt persistant, sans nouvelle tentative automatique. HTTP 429/erreurs réseau : attente globale exponentielle, respect de `Retry-After` sans plafonner la durée exigée par le serveur.
- Aucun mécanisme ne garantit l’absence de bannissement. Ne pas utiliser ce projet pour contourner un blocage.

### Conseils communautaires : ce qui est repris, ce qui est refusé

Des recommandations circulent sur Reddit pour « faire tenir » un scraper Vinted. Elles ont été triées :

| Conseil | Décision |
|---|---|
| Le catalogue exige une session (cookies obtenus par une visite normale) | **Déjà en place** : session anonyme en mémoire, jamais de cookie de compte. |
| Le jeton est renouvelé/rejeté au bout de quelques dizaines d’appels | **Repris** : 401 → renouvellement borné (3/h) au lieu d’un arrêt immédiat. |
| Réutiliser les cookies plutôt que d’ouvrir une session à chaque requête | **Repris** : la session est conservée tant que le cookie est valide, et remplacée dès qu’il expire. |
| Backoff aléatoire, espacement des requêtes | **Déjà en place** : espacement global persistant + attente exponentielle avec aléa après erreur. |
| Proxies résidentiels/mobiles rotatifs, empreintes TLS/JA3 imitant Chrome, User-Agent de navigateur, navigateur headless pour récolter des jetons | **Refusé** : ce sont des techniques de contournement des protections anti-bot d’un site dont les conditions interdisent le scraping. Elles ne sont pas implémentées et ne le seront pas ici. L’agent annoncé reste `VintedBotPersonal/0.1`. |
| Pagination au-delà de la première page / limite ~960 résultats | **Non repris** : augmenterait le volume de requêtes sans nécessité pour une veille temps réel. Une page de 96 résultats suffit ; affiner les filtres. |

Si le collecteur s’arrête malgré ces réglages, la réponse correcte est de réduire la cadence ou d’obtenir une autorisation d’accès — pas de masquer l’origine des requêtes.

## Comptabilité de gestion

Tous les montants sont stockés en **centimes entiers**, uniquement en EUR.

| Indicateur | Calcul |
|---|---|
| Coût d’un article | Achat + frais d’achat/livraison |
| Marge réalisée | Vente brute − frais de vente − remboursement au client − coût d’achat, pour les articles vendus |
| Stock | Coût des articles sans date de vente |
| Ventes brutes | Somme des prix de vente avant frais et remboursements |
| Flux net total (API) | Encaissements nets des ventes − tous les achats et frais d’achat |

La saisie représente les montants effectivement payés/reçus. Ne pas inclure dans les frais des sommes payées directement par l’autre partie. Un remboursement est ici un remboursement **au client après vente**, pas un remboursement reçu pour un achat annulé. Cette version ne gère pas les écritures de retour au stock ni les annulations d’achat automatiquement. Pour un retour client total, conserver la vente/remboursement et saisir une nouvelle fiche à coût nul pour une revente : le coût original reste imputé à la première fiche ; le total de marge est conservé mais la valorisation intermédiaire du stock retourné est nulle. Pour une comptabilité formelle des retours, utiliser votre outil comptable.

Le CSV exporte toutes les fiches, leurs dates, frais, remboursements, marge et références de justificatifs. UTF-8 avec BOM et séparateur `;`. Les textes pouvant être interprétés comme des formules sont neutralisés. Certains clients mobiles Telegram peuvent limiter le téléchargement via la WebView ; utiliser Telegram Desktop si nécessaire.

## Vérification et exploitation

```sh
python -m unittest discover -s tests -v
node --check app/static/app.js
```

Les tests couvrent la signature Telegram, l’expiration, l’isolation utilisateur, les montants, les URLs, la référence initiale, les filtres modifiés pendant une requête, les doublons/restarts, les refus robots/403, les délais 429, la file Telegram et un parcours HTTP authentifié. Voir [docs/OPERATIONS.md](docs/OPERATIONS.md) pour sauvegardes, secrets et dépannage.

## Architecture

`app/core.py` : SQLite et règles métier · `app/security.py` : HMAC Telegram · `app/workers.py` : collecte et livraison · `app/server.py` : API HTTP et lifecycle · `app/static/` : mini-app. Une seule instance Linux par base, protégée par verrou de fichier. Caddy termine HTTPS ; aucun port du service Python n’est publié par Compose. Les données persistent dans le volume `bot_data`.

Sources techniques : [validation Mini Apps](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app), [Telegram Bot API](https://core.telegram.org/bots/api), [robots.txt Vinted](https://www.vinted.fr/robots.txt).
