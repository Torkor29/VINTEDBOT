# Exploitation

## Secrets et accès

Ne jamais coller le token dans un commit, une issue ou une capture. `.env` est ignoré par Git et Docker. Configurer les vraies valeurs sur le serveur ; changer le token via BotFather en cas d’exposition. `TELEGRAM_ALLOWED_USER_IDS` constitue la liste des propriétaires autorisés. L’accès à une fiche est toujours limité à son propriétaire, même entre plusieurs IDs autorisés.

Pour lire votre ID sans mettre le token dans l’historique du shell, envoyer un message privé au bot, puis utiliser ce script interactif **avant** de lancer le service (ou après l’avoir arrêté). Il n’envoie pas de message et affiche uniquement les IDs des auteurs des messages privés reçus :

```sh
python - <<'PY'
import getpass, json, urllib.request
token = getpass.getpass('Token Telegram : ')
try:
    with urllib.request.urlopen('https://api.telegram.org/bot'+token+'/getUpdates', timeout=10) as r:
        payload = json.load(r)
    print(sorted({u['message']['from']['id'] for u in payload.get('result', [])
                  if u.get('message', {}).get('chat', {}).get('type') == 'private'}))
except Exception:
    print('Échec Telegram. Vérifiez le token, le réseau et l’absence de webhook actif.')
PY
```

Si plusieurs personnes ont écrit au bot, identifier votre propre message via un client API local sécurisé ; ne pas ajouter tous les IDs par défaut. Aucun code de développement ne contourne l’authentification.

## Sauvegarde et restauration

Le volume SQLite est l’unique source de vérité : filtres, annonces, file d’envoi, achats/ventes et paramètres de collecte. Prévoir une sauvegarde régulière hors du serveur et un chiffrement adapté. Une copie brute du seul fichier `.db` pendant l’activité n’est pas suffisante en mode WAL. Utiliser l’API SQLite de sauvegarde :

```sh
docker compose exec app python -c "import sqlite3; s=sqlite3.connect('/data/vintedbot.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp app:/data/backup.db ./backup.db
```

Le fichier de sauvegarde contient des données privées. Le transférer vers votre destination de sauvegarde, puis retirer la copie `/data/backup.db` quand elle n’est plus nécessaire. Pour restaurer : arrêter l’application, remplacer la base avec la sauvegarde, supprimer les anciens fichiers WAL/SHM **uniquement de cette base arrêtée**, vérifier le propriétaire UID 10001, puis redémarrer. Tester une restauration avant de dépendre du service.

Ne pas exécuter `docker compose down -v` : cela supprime les volumes de données. Une seule réplique ; aucun déploiement multi-instance pris en charge. Les tables de dédoublonnage et l’historique augmentent avec l’usage : surveiller l’espace disque et sauvegarder. Aucune purge automatique ne supprime vos données.

## Blocage de la collecte

L’état d’arrêt est durable, y compris après redémarrage. Lire la cause dans la mini-app. Vérifier les règles et l’autorisation d’accès, puis corriger la cause avant toute reprise. Aucun changement de proxy, cookie ou CAPTCHA n’est proposé.

Après résolution confirmée, un opérateur peut effacer l’état d’arrêt avec la collecte arrêtée :

```sh
docker compose stop app
docker compose run --rm --no-deps app python -c "from app.core import Store; s=Store('/data/vintedbot.db'); s.set('collector_halted',''); s.set('collector_error','')"
docker compose up -d app
```

Cela conserve la prochaine échéance globale (y compris `Retry-After`). Si l’accès est toujours refusé, l’arrêt sera rétabli. Si `robots.txt` ne peut pas être vérifié, le catalogue n’est pas interrogé.

## Telegram

`getUpdates` utilise le long polling. Ne pas configurer de webhook pour ce bot et ne pas lancer deux pollers. Un ancien webhook doit être retiré volontairement via BotFather/API Telegram avant le lancement ; ce projet ne supprime pas automatiquement vos réglages existants. HTTP 409 dans les logs indique généralement un conflit webhook/poller. Les logs omettent les tokens, URLs sensibles et données comptables.

Le bot répond uniquement aux propriétaires autorisés en privé. Les destinataires des notifications proviennent d’une commande `/start` vérifiée, jamais d’un `chat_id` envoyé par le navigateur. `/stop` coupe les envois mais conserve les filtres et les annonces en attente ; `/start` peut donc entraîner l’envoi de ces annonces accumulées. Mettre aussi les filtres en pause si vous ne souhaitez plus collecter.

Livraison : dédoublonnage des annonces en base, puis envoi Telegram. Après une panne au moment précis où Telegram accepte un message mais avant l’enregistrement local du succès, une répétition est possible : Telegram `sendMessage` n’offre pas de clé d’idempotence. Au maximum 12 tentatives pour un message ; HTTP 400 est terminal, HTTP 403 désactive le chat. Les alertes échouées restent consultables dans la mini-app. Les échecs globaux du token ou du polling sont visibles dans les logs ; `/healthz` mesure seulement la disponibilité HTTP, pas la disponibilité de Telegram/Vinted.

## Déploiement

Le fichier Compose est préparé mais doit être exécuté sur votre serveur. La mini-app nécessite une origine HTTPS publique, un token et votre ID Telegram : un simple hébergement de fichiers statiques ne suffit pas. Le mode standard utilise un domaine/DNS et Caddy. Pour les premiers essais sans acheter de domaine, utiliser [compose.tunnel.yaml et le guide VPS](QUICKSTART_VPS.md) ; son adresse HTTPS temporaire doit être mise à jour après redémarrage du tunnel. Dans ce mode, ajouter `-f compose.tunnel.yaml` à **toutes** les commandes Compose de ce guide. Aucun serveur ni domaine n’est acheté ou créé par ce dépôt. Le code Python s’exécute aussi hors Docker sur Linux : exporter les variables nécessaires, donner un chemin inscriptible à `DATABASE_PATH`, puis `python -m app.server` derrière un proxy HTTPS.

Python est sans paquet tiers. Les images Docker et les actions CI utilisent des versions majeures ; pour une exploitation durable, appliquer régulièrement les mises à jour de sécurité et figer des digests vérifiés dans votre environnement.
