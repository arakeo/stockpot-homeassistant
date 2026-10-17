# Stockpot pour Home Assistant

Intégration personnalisée (HACS) de [Stockpot](https://stockpot.arakeo.com) : listes de courses, planning des repas, alertes de stock et actions.

- **Home Assistant** : 2026.10 ou plus récent (voir `hacs.json`).
- **Stockpot** : M8 ou plus récent (API `/api/v1`, endpoints `/ha/*`).

## Installation

1. HACS > Intégrations > ⋮ > Dépôts personnalisés : ajoutez ce dépôt (catégorie « Intégration »), puis installez **Stockpot**. Sans HACS : copiez `custom_components/stockpot` dans le dossier `config/custom_components/` de Home Assistant.
2. Redémarrez Home Assistant.
3. Dans Stockpot : **Réglages > Home Assistant > Créer un jeton** (capacité `ha`, affiché une seule fois).
4. Dans Home Assistant : **Paramètres > Appareils et services > Ajouter une intégration > Stockpot** : URL de Stockpot, jeton, vérification SSL, puis le foyer si le compte en a plusieurs.

Le jeton `ha` est **lié à un foyer** et ne donne que ce qu'utilise l'intégration : lecture des courses et du planning, écritures utiles (courses, consommer / ajouter au stock, planifier, cuisiné, recalculer la liste), jamais l'administration.

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

## Actions

`stockpot.consume_product`, `stockpot.add_stock` (par `ean` — quantité en conditionnements — ou `product_id`), `stockpot.add_to_shopping_list`, `stockpot.plan_meal`, `stockpot.cook_meal`, `stockpot.generate_shopping_list`. Chaque action envoie une clé `Idempotency-Key` neuve, réutilisée pour au plus 2 nouvelles tentatives (délai dépassé, erreur réseau, 5xx) : Stockpot ne l'applique jamais deux fois.

## Rafraîchissement

Interrogation toutes les 60 s, plus un **webhook** : au démarrage, l'intégration enregistre un webhook Home Assistant et donne son URL à Stockpot (jeton du **propriétaire** du foyer). Stockpot n'appelle que les hôtes de sa variable `STOCKPOT_HA_WEBHOOK_HOSTS` et signe chaque notification (`X-Stockpot-Signature: sha256=HMAC(secret, "<timestamp>.<corps>")`, `X-Stockpot-Timestamp`, fenêtre de 5 min). Sans webhook, l'interrogation suffit.

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
