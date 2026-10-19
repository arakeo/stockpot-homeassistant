# Stockpot pour Home Assistant

Intégration personnalisée (HACS) de [Stockpot](https://stockpot.arakeo.com) : listes de courses, planning des repas, alertes de stock et actions.

- **Home Assistant** : 2026.10 ou plus récent (voir `hacs.json`).
- **Stockpot** : l'intégration se connecte à **`https://stockpot.arakeo.com`** (constante `STOCKPOT_URL` de `const.py`), certificat TLS toujours vérifié. Les nouvelles entrées exigent Stockpot M17 ou plus récent (connexion OAuth2 de Home Assistant).

## Installation

1. HACS > Intégrations > ⋮ > Dépôts personnalisés : ajoutez ce dépôt (catégorie « Intégration »), puis installez **Stockpot**. Sans HACS : copiez `custom_components/stockpot` dans le dossier `config/custom_components/` de Home Assistant.
2. Redémarrez Home Assistant.
3. **Paramètres > Appareils et services > Ajouter l'intégration > Stockpot → Se connecter → Autoriser.** Rien à saisir : ni adresse, ni option SSL, ni méthode de connexion.

Home Assistant lit le document de découverte de Stockpot (`/.well-known/oauth-authorization-server`) et ouvre Stockpot dans un nouvel onglet : connexion habituelle (Google + double authentification), choix du foyer, **Autoriser**. L'onglet se ferme et l'intégration est créée pour ce foyer. Aucun jeton à copier : l'accès est renouvelé automatiquement (jeton d'accès d'une heure, refresh token tournant). Pour un second foyer, ajoutez l'intégration une seconde fois.

- **Prérequis : l'intégration [My Home Assistant](https://www.home-assistant.io/integrations/my/)** (`my`, incluse dans `default_config`). Stockpot renvoie le navigateur vers `https://my.home-assistant.io/redirect/oauth`, qui le ramène à votre Home Assistant. Sans `my`, l'assistant affiche un avertissement : Home Assistant utiliserait `https://<votre-ha>/auth/external/callback`, que Stockpot n'accepte que si l'hôte figure dans `STOCKPOT_OAUTH_HA_REDIRECT_HOSTS` ; validez à nouveau pour continuer dans ce cas.
- Si la connexion ne peut pas démarrer (Stockpot injoignable, certificat refusé…), l'assistant affiche l'erreur ; validez pour réessayer.
- Si l'accès est révoqué (Stockpot > Réglages > Home Assistant > Déconnecter), expiré (90 jours sans usage) ou si le compte quitte le foyer, Home Assistant demande de **se reconnecter** (réauthentification) ; l'ancien accès est alors révoqué. **Reconfigurer** fait la même chose (nouvelle connexion, même foyer) : l'adresse d'une nouvelle entrée n'est pas modifiable.
- **Supprimer l'intégration** révoque l'accès dans Stockpot (RFC 7009), avec le webhook qu'il avait enregistré ; si la révocation échoue, l'intégration retire au moins le webhook.

L'accès est **lié à un foyer** et ne donne que ce qu'utilise l'intégration : lecture des courses et du planning, écritures utiles (courses, consommer / ajouter au stock, planifier, cuisiné, recalculer la liste), jamais l'administration.

### Entrées créées avant la 0.11.0

Le **jeton manuel** n'est plus proposé pour une nouvelle installation. Les entrées existantes continuent de fonctionner sans changement, avec leurs propres réglages :

- **Jeton manuel** (0.9 / 0.10) : l'entrée garde son jeton. Quand Home Assistant demande de se reconnecter, un menu propose « Se connecter avec Stockpot » (à l'adresse de l'entrée) ou « Coller un nouveau jeton » (si ce Home Assistant ne peut pas terminer la connexion : pas de `my`, hôte non autorisé par Stockpot). En cas de connexion réussie (même instance, même foyer), l'entrée passe en OAuth ; l'ancien jeton n'est pas révoqué, supprimez-le dans Stockpot > Réglages > Home Assistant. Lors de cette reconnexion uniquement, si ce Stockpot ne propose pas la connexion de Home Assistant, le formulaire de nouveau jeton s'affiche directement. **Reconfigurer** propose le même menu, l'option jeton permettant aussi, comme en 0.10, de modifier l'adresse et la vérification SSL : seules ces entrées peuvent changer d'adresse.
- **OAuth avec une autre adresse ou sans vérification SSL** (0.10.0) : l'entrée garde son adresse et son réglage SSL, pour le rafraîchissement comme pour la reconnexion. Pour ces adresses, une instance en `https` qui annonce un point de connexion en `http` est refusée, et le `http` simple n'est accepté que pour une adresse locale (loopback, réseau privé, nom sans point, `.local`, `.lan`, `.home.arpa`, `.internal`). L'adresse n'est pas modifiable, et une nouvelle entrée ne peut viser que `stockpot.arakeo.com`.
- **Webhook** : Stockpot n'en garde qu'un par foyer (le dernier enregistré l'emporte) ; après le passage en OAuth, l'entrée le réenregistre avec son nouvel accès et un nouveau secret.

### Logo

Le logo Stockpot est fourni dans `custom_components/stockpot/brand/` (`icon.png` 256 px, `icon@2x.png` 512 px, `logo.png` / `logo@2x.png` 128 / 256 px de haut, variantes `dark_*`) : Home Assistant 2026.3+ les sert localement (tuile de l'intégration, assistant, appareils), et HACS les accepte pour sa vérification des marques.

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

`stockpot.consume_product`, `stockpot.add_stock` (par `ean` — quantité en conditionnements — ou `product_id` ; depuis la 0.13.0, un `ean` envoie aussi l'**article** de cet EAN, pour que le stock connaisse la marque et le format — Stockpot M20b ; un article archivé est refusé pour un ajout seulement), `stockpot.add_to_shopping_list` (par `ean` : l'article scanné devient l'article préféré de la ligne, sauf s'il est archivé), `stockpot.plan_meal`, `stockpot.cook_meal`, `stockpot.generate_shopping_list`. Chaque action envoie une clé `Idempotency-Key` neuve, réutilisée pour au plus 2 nouvelles tentatives (délai dépassé, erreur réseau, 5xx) : Stockpot ne l'applique jamais deux fois.

## Rafraîchissement

Interrogation toutes les 60 s, plus un **webhook** : au démarrage, l'intégration enregistre un webhook Home Assistant et donne son URL à Stockpot (accès du **propriétaire** du foyer ; pour un membre, Stockpot répond 403 et l'intégration se contente de l'interrogation). Stockpot n'appelle que les hôtes de sa variable `STOCKPOT_HA_WEBHOOK_HOSTS` et signe chaque notification (`X-Stockpot-Signature: sha256=HMAC(secret, "<timestamp>.<corps>")`, `X-Stockpot-Timestamp`, fenêtre de 5 min). Sans webhook, l'interrogation suffit.

## Panneau Stockpot (0.12.0)

**Stockpot entier dans la barre latérale** (entrée « Stockpot », icône `mdi:pot-steam`), sans connexion dans Home Assistant : dans un navigateur, dans l'application compagnon (Wi-Fi ou 4G), via `homeassistant.local`, une IP ou Nabu Casa. Exige Stockpot M18 ou plus récent.

- **Activation** : case « Afficher Stockpot dans Home Assistant » sur l'écran de consentement de Stockpot (cochée par défaut), modifiable ensuite dans Stockpot › Réglages › Home Assistant. **Mise à jour** : une connexion faite avant Stockpot M18 a le panneau désactivé (le consentement d'alors ne le prévoyait pas) ; le panneau affiche « Panneau Stockpot désactivé » tant que la case n'est pas cochée dans Stockpot › Réglages › Home Assistant.
- **Options de l'intégration** (Paramètres › Appareils et services › Stockpot › Configurer) : « Afficher le panneau » (oui par défaut), « Réservé aux administrateurs » (non par défaut : visible par tous les utilisateurs de Home Assistant).
- **Compte** : les actions faites dans le panneau sont attribuées au compte Stockpot qui a connecté l'intégration, quel que soit l'utilisateur de Home Assistant.
- **Périmètre** : toutes les données du foyer (recettes, articles, planning, courses, stock, scan, photo du stock, tickets, commandes), en lecture et écriture. L'administration (réglages du foyer, personnes, invitations, double authentification, jetons, connecteur Claude, coût de Claude, administration du catalogue) affiche « Ouvrir dans Stockpot » (nouvel onglet).
- **Fonctionnement** : le panneau (`frontend/stockpot-panel.js`, élément `<stockpot-panel>`) contient une iframe de `<Stockpot>/embed`. Stockpot demande un code à usage unique (60 s) que le panneau obtient par la commande websocket `stockpot/panel/code` : l'intégration appelle `POST /api/v1/ha/panel-codes` avec son accès OAuth. Stockpot l'échange contre une session d'une heure, gardée en mémoire et renouvelée sans rien demander. Aucun cookie, aucun mot de passe dans Home Assistant. Les messages ne sont échangés qu'avec l'origine de Stockpot.
- **Plusieurs foyers** : un panneau par entrée (`/stockpot`, puis `/stockpot_<id>`), titré « Stockpot · <foyer> ».
- **Entrées à jeton manuel** : pas de panneau (Stockpot ne l'ouvre que pour une connexion OAuth). Une réparation propose « Reconfigurer › Se connecter avec Stockpot » ; désactiver le panneau dans les options la fait disparaître.
- Si le panneau est désactivé côté Stockpot, il l'explique avec un lien vers les réglages de Stockpot.

## Carte « Page web » (iframe)

Préférez le **panneau Stockpot** ci-dessus. Les vues `/ha/planning`, `/ha/courses`, `/ha/stock` de Stockpot s'affichent dans une carte `type: iframe` (`allow: "camera; fullscreen"`) **seulement dans un navigateur déjà connecté à Stockpot sur le même site** (ex. `ha.arakeo.com` / `stockpot.arakeo.com`) : pas dans l'application compagnon, ni via `homeassistant.local`, une IP ou Nabu Casa. Dans l'application compagnon, utilisez les entités de l'intégration.

## Exemples

`examples/` : rappel de la liste en arrivant au magasin, péremptions à 18 h, carte du repas du jour. Les identifiants d'entités supposent un Home Assistant en français et un foyer « Foyer Martin » : adaptez-les.

## Développement

```bash
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```
