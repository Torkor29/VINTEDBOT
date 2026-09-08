# Premiers essais sur VPS sans acheter de domaine

Le bot utilise une connexion sortante vers Telegram ; il n'a pas besoin de recevoir un webhook. La **mini-app** a néanmoins besoin d'une URL HTTPS publique ([Telegram WebAppInfo](https://core.telegram.org/bots/api#webappinfo)). Une IP en HTTP ne suffit pas.

Cette configuration utilise un [Quick Tunnel Cloudflare](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/) gratuit, sans compte ni domaine personnel. Il fournit une adresse aléatoire `https://….trycloudflare.com`. Cloudflare le réserve aux essais et ne garantit pas sa disponibilité. L'adresse change si le processus du tunnel redémarre. Cloudflare relaie les accès à la mini-app ; les requêtes sortantes vers Vinted continuent à partir directement du VPS. Ce tunnel ne contourne aucun blocage de Vinted.

## 1. Préparer le projet

Prérequis : VPS Linux, Docker et plugin Docker Compose installés ; connexions sortantes autorisées vers Telegram, Vinted et Cloudflare. Aucun port HTTP du bot n'est publié sur le VPS avec cette variante. Garder votre accès SSH habituel.

```sh
git clone https://github.com/Torkor29/VINTEDBOT.git
cd VINTEDBOT
cp .env.example .env
```

Si le dépôt existe déjà, utiliser `git pull --ff-only` et **conserver** votre `.env`. Renseigner `TELEGRAM_BOT_TOKEN` et votre ID numérique dans `TELEGRAM_ALLOWED_USER_IDS`. Voir [OPERATIONS.md](OPERATIONS.md) pour récupérer cet ID. Ne pas partager le token. `DOMAIN` n'est pas utilisé en mode tunnel ; `PUBLIC_URL` sera renseigné à l'étape suivante.

## 2. Obtenir l'adresse HTTPS

Démarrer uniquement le tunnel : il affichera son adresse avant le démarrage de l'application. Les accès à cette adresse échoueront provisoirement tant que l'application n'est pas lancée.

```sh
docker compose -f compose.tunnel.yaml up -d tunnel
docker compose -f compose.tunnel.yaml logs --tail=100 tunnel
```

Copier l'adresse `https://….trycloudflare.com` affichée dans `PUBLIC_URL` dans `.env`, sans slash final ni chemin. Ne pas employer une adresse d'exemple.

## 3. Lancer l'application

```sh
docker compose -f compose.tunnel.yaml up -d --build app
docker compose -f compose.tunnel.yaml ps
```

Renseigner la même URL dans la configuration de menu/Mini App de BotFather, puis envoyer `/start` en privé au bot. Ouvrir le nouveau bouton « Mon espace ». L'authentification Telegram et la liste des propriétaires autorisés restent obligatoires, même avec l'adresse publique du tunnel.

## 4. Choisir 15 secondes

- Un **nouveau** filtre utilise désormais 15 secondes par défaut. Les filtres existants conservent leur fréquence : ouvrir « Modifier », choisir « Toutes les 15 secondes », enregistrer. Changer seulement la fréquence ne remet pas à zéro la référence ni le dédoublonnage.
- Si votre `.env` date de la première version, remplacer `GLOBAL_REQUEST_GAP_SECONDS=60` par `GLOBAL_REQUEST_GAP_SECONDS=1`, puis recréer `app` avec la commande ci-dessus. Garder 60 imposerait encore un espacement global d'une minute.
- La collecte demeure désactivée tant que l'accès à Vinted n'a pas été validé et autorisé, puis configuré avec les deux variables d'activation décrites dans le README. Ni le tunnel ni ce changement de cadence n'activent la collecte.
- 15 secondes est une **échéance de recherche**, pas une promesse d'annonce reçue en 15 secondes. Le collecteur travaille en série, au maximum une requête par seconde avec le réglage fourni ; le nombre de filtres, le temps réseau, les indications de `robots.txt` et `Retry-After` peuvent augmenter le délai. Une requête HTTP refusée ne déclenche pas de contournement.

## Après un redémarrage du tunnel

Lire sa nouvelle adresse dans les logs, mettre à jour `PUBLIC_URL`, recréer **uniquement app**, modifier le menu/Mini App dans BotFather et envoyer à nouveau `/start`. Les anciens boutons peuvent pointer vers l'ancienne adresse. Le redémarrage d'app seul ne change pas l'adresse tant que le tunnel reste en cours.

Pour un fonctionnement permanent, remplacer cette adresse temporaire par une origine HTTPS stable. Un domaine personnel n'est pas nécessaire aux premiers essais, mais une adresse stable évite ces reconfigurations.

## Exploitation

Toujours utiliser `docker compose -f compose.tunnel.yaml …` dans ce mode, y compris pour les commandes de sauvegarde et de diagnostic d'OPERATIONS.md. Ne pas lancer le fichier Compose standard en parallèle. Le volume `bot_data` est conservé dans le même projet Compose ; ne pas utiliser `down -v`. Ne pas changer arbitrairement le nom du projet lors d'une mise à jour, sous peine de créer un nouveau volume vide.

La configuration du tunnel est préparée dans le dépôt ; elle n'a pas été exécutée sur votre VPS pendant le développement. Aucun accès VPS ni token Telegram n'a été fourni à l'assistant.
