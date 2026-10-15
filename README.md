# Serveur MCP — Réservation de vols ENVIRECO

Serveur MCP distant qui permet à ChatGPT Business de rechercher et réserver des billets d'avion via l'API Duffel, et d'écrire dans la base Airtable ENVIRECO. Il remplace les scénarios Make conservés dans `reference_make/`.

**Une réservation déclenche un paiement réel sur le solde Duffel.** Faites tous vos essais avec une clé `duffel_test_…`.

## Outils exposés

| Outil | Rôle | Écrit ? |
|---|---|---|
| `rechercher_vols` | Offres aller simple ou aller-retour, classe au choix | non |
| `reserver_vol` | Réserve et paie l'offre, puis l'enregistre dans `reservation_vol` | **oui, paiement** |
| `enregistrer_reservation` | Rattrapage : écrit dans `reservation_vol` une commande déjà payée | oui |
| `consulter_reservations` | Vols réservés des prochains mois | non |
| `lister_passagers` | Lit `Coordonnees_passager_vol` | non |
| `ajouter_passager` | Ajoute un passager dans `Coordonnees_passager_vol` | oui |

Garde-fous de `reserver_vol` : le serveur relit l'offre chez Duffel et refuse si le prix ou la devise diffèrent du montant confirmé, si l'offre a expiré ou si les identifiants passagers ne sont pas ceux de l'offre. Un second appel sur la même offre ne réserve pas deux fois. Si Airtable échoue après le paiement, la réservation est quand même renvoyée comme réussie, avec `enregistre_dans_airtable: false`.

## Variables d'environnement

| Variable | Rôle |
|---|---|
| `DUFFEL_API_KEY` | Clé Duffel (`duffel_test_…` puis `duffel_live_…`) |
| `AIRTABLE_TOKEN` | Personal Access Token Airtable, limité à la base, scopes `data.records:read` et `data.records:write` |
| `AIRTABLE_BASE_ID` | `appOaaEm9hZALEfBz` |
| `MCP_LOGIN_PASSWORD` | Mot de passe demandé à la connexion du connecteur (12 caractères minimum) |
| `JWT_SECRET` | Secret de signature des jetons (32 caractères minimum) |
| `PUBLIC_BASE_URL` | URL publique du serveur, sans barre finale |

Le serveur refuse de démarrer si l'une d'elles manque. Changer `MCP_LOGIN_PASSWORD` ou `JWT_SECRET` déconnecte ChatGPT : il suffit de reconnecter le connecteur.

## Lancer en local

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env      # puis renseigner les valeurs
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m src.server
```

Le serveur écoute sur `http://localhost:8000/mcp`. Pour le tester avec MCP Inspector (`npx @modelcontextprotocol/inspector`), ajoutez `OAUTH_ALLOWED_REDIRECT_HOSTS=chatgpt.com,chat.openai.com,localhost` dans `.env`.

## Déployer sur Render

1. Installer Git, créer un dépôt GitHub **privé** et y pousser ce dossier (`.env` et `.venv` sont ignorés).
2. Dans Render : **New > Blueprint**, choisir le dépôt. `render.yaml` crée le service web.
3. Renseigner `DUFFEL_API_KEY` (clé de test), `AIRTABLE_TOKEN` et `MCP_LOGIN_PASSWORD`. `JWT_SECRET` est généré par Render.
4. Après le premier déploiement, copier l'URL du service (`https://….onrender.com`) dans `PUBLIC_BASE_URL` et redéployer.
5. Vérifier `https://….onrender.com/health` → `{"status":"ok"}`.

`render.yaml` utilise l'offre **Starter** : l'offre gratuite endort le service après 15 minutes et le réveil dépasse souvent le délai d'attente de ChatGPT.

## Connecter ChatGPT Business

1. Un administrateur de l'espace de travail active le **mode développeur** (réglages des connecteurs / applications).
2. Créer un connecteur personnalisé : URL `https://….onrender.com/mcp`, authentification **OAuth**.
3. ChatGPT ouvre la page de connexion du serveur : saisir `MCP_LOGIN_PASSWORD`.
4. Coller le contenu de `src/instructions.py` (le texte entre guillemets triples) dans les instructions du projet ou du GPT qui utilisera le connecteur.

Le mode développeur et les actions d'écriture MCP de ChatGPT sont en bêta : les intitulés des menus peuvent différer.

## À vérifier lors des premiers essais (mode test)

- **Champs de `Coordonnees_passager_vol`.** Les noms utilisés (`Nom`, `Prénom`, `Titre`, `Genre`, `Date_naissance`, `Email`, `Téléphone`) viennent de l'instruction de l'agent Make, pas de la base elle-même. S'ils diffèrent, corriger `CHAMPS_PASSAGER` dans `src/airtable.py` et l'outil `ajouter_passager` dans `src/server.py`.
- **Heure dans `reservation_vol`.** Le champ `Date` reçoit l'heure locale de départ telle quelle (marquée UTC). Si le champ Airtable est affiché dans un autre fuseau, l'heure apparaîtra décalée : régler le champ sur « GMT/UTC, même fuseau pour tous ».
- **Réservation de bout en bout.** Réserver une offre de la compagnie fictive « Duffel Airways », vérifier la commande dans le tableau de bord Duffel (mode test) et les lignes créées dans `reservation_vol`, puis supprimer ces lignes.

## Passer en production

Remplacer `DUFFEL_API_KEY` par la clé `duffel_live_…` dans Render, vérifier que le solde Duffel est approvisionné, puis faire une première réservation réelle en la supervisant.
