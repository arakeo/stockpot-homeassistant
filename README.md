# Stockpot pour Home Assistant

Intégration personnalisée (HACS) de [Stockpot](https://stockpot.arakeo.com) : listes de courses, planning des repas, alertes de stock et actions.

- **Home Assistant** : 2026.10 ou plus récent (voir `hacs.json`).
- **Stockpot** : M8 ou plus récent (API `/api/v1`, endpoints `/ha/*`) pour le jeton manuel. « Se connecter avec Stockpot » (OAuth2) exige **Stockpot M17** tel que fusionné (PR #27 : un accès distinct par connexion). La version 0.10.0 n'est publiée qu'après le déploiement de M17.

## Installation

1. HACS > Intégrations > ⋮ > Dépôts personnalisés : ajoutez ce dépôt (catégorie « Intégration »), puis installez **Stockpot**. Sans HACS : copiez `custom_components/stockpot` dans le dossier `config/custom_components/` de Home Assistant.
2. Redémarrez Home Assistant.
3. Dans Home Assistant : **Paramètres > Appareils et services > Ajouter une intégration > Stockpot** : URL de Stockpot (ex. `https://stockpot.arakeo.com`), vérification SSL, puis la méthode de connexion.

### Se connecter avec Stockpot (recommandé)

Home Assistant lit le document de découverte de l'instance (`/.well-known/oauth-authorization-server`) et ouvre Stockpot dans un nouvel onglet : connexion habituelle (Google + double authentification), choix du foyer, **Autoriser**. L'onglet se ferme et l'intégration est créée pour ce foyer. Aucun jeton à copier : l'accès est renouvelé automatiquement (jeton d'accès d'une heure, refresh token tournant).

- **Prérequis : l'intégration [My Home Assistant](https://www.home-assistant.io/integrations/my/)** (`my`, incluse dans `default_config`). Stockpot renvoie le navigateur vers `https://my.home-assistant.io/redirect/oauth`, qui le ramène à votre Home Assistant. Sans `my`, l'assistant affiche un avertissement : Home Assistant utiliserait `https://<votre-ha>/auth/external/callback`, que Stockpot n'accepte que si l'hôte figure dans `STOCKPOT_OAUTH_HA_REDIRECT_HOSTS` ; validez à nouveau pour continuer dans ce cas.
- **Transport** : une instance en `https` qui annonce un point de connexion en `http` est refusée ; le `http` simple n'est accepté que pour une adresse locale (loopback, réseau privé, nom sans point, `.local`, `.lan`, `.home.arpa`, `.internal`). Une adresse locale saisie alors que Stockpot annonce son adresse publique (`APP_URL`) est acceptée, avec un avertissement dans le journal.
- Si l'accès est révoqué (Stockpot > Réglages > Home Assistant > Déconnecter), expiré (90 jours sans usage) ou si le compte quitte le foyer, Home Assistant demande de **se reconnecter** (réauthentification) ; l'ancien accès est alors révoqué.
- **Supprimer l'intégration** révoque l'accès dans Stockpot (RFC 7009), avec le webhook qu'il avait enregistré ; si la révocation échoue, l'intégration retire au moins le webhook.

### Jeton manuel (secours)

Pour un Stockpot antérieur à M17, une instance Home Assistant sans `my`, ou des scripts : dans Stockpot, **Réglages > Home Assistant > Créer un jeton** (capacité `ha`, affiché une seule fois), puis choisissez « Jeton manuel » et collez-le ; choisissez le foyer si le compte en a plusieurs.

Dans les deux cas, l'accès est **lié à un foyer** et ne donne que ce qu'utilise l'intégration : lecture des courses et du planning, écritures utiles (courses, consommer / ajouter au stock, planifier, cuisiné, recalculer la liste), jamais l'administration.

## Entités

| Entité | Contenu |
|---|---|
| `todo.<foyer>_liste_de_courses` (+ une par liste secondaire) | lignes à acheter ; cocher = « Reçu » (l'article entre au stock) ; ajout (texte libre), renommage (texte libre), note, suppression, déplacement |
| `calendar.<foyer>_repas`, `calendar.<foyer>_dejeuner`, `calendar.<foyer>_diner`… | repas planifiés (un calendrier par type de repas actif) |
| `sensor.<foyer>_articles_a_acheter` | nombre d'articles à acheter (attribut `stale` : liste pas encore recalculée par Stockpot) |
| `sensor.<foyer>_stock_bas` | nombre d'articles sous leur seuil, liste en attribut `products` |
| `sensor.<foyer>_peremptions_proches` | produits périmés ou bientôt, liste en attribut `products` |
| `sensor.<foyer>_prochain_repas` | recette ou note, attributs `servings`, `present`, `starts_at` |
| `binary_sensor.<foyer>_commande_drive_a_passer` | liste non vide et jour de commande Drive atteint |
| `binary_sensor.<foyer>_commande_a_recuperer` | commande Drive passée en attente de retrait (attributs `order_number`, `pickup_at`, `lines_count`, `total`) |
| `sensor.<foyer>_sante_de_stockpot` (diagnostic) | `ok` / `degraded` (Stockpot M14+) |
| `sensor.<foyer>_taches_en_echec_24_h` (diagnostic) | jobs en échec sur 24 h, toute l'instance (Stockpot M14+) |
| `sensor.<foyer>_cout_claude_du_mois` | coût Claude estimé du foyer pour le mois en cours, en USD ; attributs `cap_usd` (plafond du foyer, `null` sans plafond) et `blocked` (appels Claude refusés) (Stockpot M14+) |

Les trois capteurs de santé sont indisponibles avec une version de Stockpot antérieure à M14 (pas de bloc `health`).

## Actions

`stockpot.consume_product`, `stockpot.add_stock` (par `ean` — quantité en conditionnements — ou `product_id`), `stockpot.add_to_shopping_list`, `stockpot.plan_meal`, `stockpot.cook_meal`, `stockpot.generate_shopping_list`. Chaque action envoie une clé `Idempotency-Key` neuve, réutilisée pour au plus 2 nouvelles tentatives (délai dépassé, erreur réseau, 5xx) : Stockpot ne l'applique jamais deux fois.

## Rafraîchissement

Interrogation toutes les 60 s, plus un **webhook** : au démarrage, l'intégration enregistre un webhook Home Assistant et donne son URL à Stockpot (accès du **propriétaire** du foyer ; pour un membre, Stockpot répond 403 et l'intégration se contente de l'interrogation). Stockpot n'appelle que les hôtes de sa variable `STOCKPOT_HA_WEBHOOK_HOSTS` et signe chaque notification (`X-Stockpot-Signature: sha256=HMAC(secret, "<timestamp>.<corps>")`, `X-Stockpot-Timestamp`, fenêtre de 5 min). Sans webhook, l'interrogation suffit.

## Carte « Page web » (iframe)

Les vues `/ha/planning`, `/ha/courses`, `/ha/stock` de Stockpot s'affichent dans une carte `type: iframe` (`allow: "camera; fullscreen"`) **seulement dans un navigateur déjà connecté à Stockpot sur le même site** (ex. `ha.arakeo.com` / `stockpot.arakeo.com`) : pas dans l'application compagnon, ni via `homeassistant.local`, une IP ou Nabu Casa. Dans l'application compagnon, utilisez les entités de l'intégration.

## Exemples

`examples/` : rappel de la liste en arrivant au magasin, péremptions à 18 h, carte du repas du jour. Les identifiants d'entités supposent un Home Assistant en français et un foyer « Foyer Martin » : adaptez-les.

## Développement

```bash
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```
