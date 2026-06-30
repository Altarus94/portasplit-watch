# PortaSplit Watch — alerte stock gratuite sur ton téléphone

Un petit robot qui surveille le **Midea PortaSplit 12000** et t'envoie une **notif push** dès qu'il revient en stock **près de chez toi**, **au prix normal** (≤ 1100 €). Il tourne tout seul **toutes les ~10 min**, **sans ton PC allumé**, et c'est **100 % gratuit**.

Il s'appuie sur **ClimRadar** (climradar.fr), qui suit en direct le stock **magasin par magasin** d'une douzaine d'enseignes (Castorama, Leroy Merlin, ManoMano, Boulanger, Fnac, Bricoman…). Le robot ne t'alerte que si le PortaSplit est réellement disponible **dans un magasin à ≤ 40 km de chez toi** (secteur 94100) **ou en ligne pour livraison** — et seulement au passage *rupture → dispo*, magasin par magasin (pas de spam). Le rayon et le périmètre se règlent dans `watch.yml`.

**Amazon** est géré à part, via **Keepa** (voir plus bas) : Amazon bloque le scraping côté serveur, et Keepa fait le filtre de prix + le retour en stock proprement.

---

## Ce qu'il te faut
1. Ton téléphone (l'appli **ntfy**, gratuite).
2. Un compte **GitHub** gratuit (c'est lui qui héberge et lance le robot).

---

## 1) L'appli de notif — 2 min
1. Installe **ntfy** depuis le Play Store.
2. Ouvre-la → bouton **+** → invente un **nom de sujet (topic)** unique et difficile à deviner, par ex. `portasplit-altarus-7h2k9`.
3. Note-le. Tout message envoyé à ce topic arrivera sur ton tél. C'est tout.

> Astuce : un topic est public pour qui connaît son nom. Prends un nom long et aléatoire.

## 2) Créer le robot sur GitHub — 5 min
1. Crée un compte sur **github.com**.
2. En haut à droite : **+ → New repository**. Nom : `portasplit-watch`. Coche **Private**. **Create**.
3. Envoie les fichiers de ce dossier dans le repo :
   - **Add file → Upload files** → glisse `watch.py`, `requirements.txt`, `state.json`.
   - Pour le workflow : **Add file → Create new file**, et tape comme nom exactement :
     `.github/workflows/watch.yml`
     (les `/` créent les dossiers). Colle dedans le contenu du `watch.yml` fourni. **Commit**.

## 3) Mettre ton topic — 1 min
Le topic ntfy est rangé dans un **secret** du dépôt (jamais dans le code).
1. Sur GitHub : **Settings → Secrets and variables → Actions → New repository secret**.
2. Nom : `NTFY_TOPIC` — Valeur : ton topic. **Add secret**.
3. (optionnel) ajuste `WATCH_RADIUS_KM`, `INCLUDE_ONLINE` ou `PRICE_MAX` dans `watch.yml`.

## 4) Activer et tester
1. Onglet **Actions** → si demandé, clique pour autoriser les workflows.
2. Clique le workflow **PortaSplit watch** → bouton **Run workflow** (test immédiat).
3. Si un stock est déjà dispo sous le seuil, tu reçois la notif. Sinon, rien (c'est normal).
4. Ensuite, ça tourne tout seul **toutes les ~10 min**. 🎉

---

## Régler / mettre en pause
- **Rayon, périmètre, prix** : édite dans `watch.yml` → `WATCH_RADIUS_KM` (distance en km), `INCLUDE_ONLINE` (`1` = aussi les dispos en ligne, `0` = magasins proches seulement), `PRICE_MAX` (prix plafond). Si tu déménages, change aussi `WATCH_LAT` / `WATCH_LON` / `WATCH_POSTAL`.
- **Pause** : onglet Actions → le workflow → **⋯ → Disable workflow**. Pour relancer : **Enable**.
- **Tu l'as acheté ?** Disable, ou supprime le repo.

## Bon à savoir
- GitHub lance le cron **« vers »** toutes les ~10 min (souvent quelques minutes de retard en pic de charge). Dépôt **public** → minutes d'Actions **illimitées et gratuites**.
- Le stock vient de **ClimRadar**, qui agrège les enseignes : plus besoin de scraper chaque site (donc fini les blocages 403). Si ClimRadar est momentanément indisponible, le robot garde son état et réessaie au passage suivant.
- C'est une photo du **stock magasin** au moment du passage : vérifie quand même la dispo et le prix sur place (ou sur le site) avant de te déplacer.
- La notif ouvre ta **vue ClimRadar géolocalisée** (secteur 94100) — pratique pour le retrait magasin.

## Amazon en plus, via Keepa — 2 min
Amazon se prête mal au scraping (mur anti-robot, réponse vide aux serveurs). Pour le couvrir **avec le filtre de prix** :
1. Installe l'appli **Keepa** (Android) ou l'extension navigateur.
2. Ouvre le produit : ASIN **B0CY2YW8BT** (`amazon.fr/dp/B0CY2YW8BT`).
3. **Track product** → *Desired price* = **999 €**, condition **Neuf**.
4. Active la notif (appli Keepa, ou Telegram). Tu n'es prévenu que **sous 999 €** → les vendeurs gonflés sont filtrés.

## Pas envie de bricoler GitHub ?
Demande à **Claude Code** sur ton PC : il crée le repo, pousse les fichiers et active le workflow pour toi d'un coup. (Ou on passe à la version « vraie app Android ».)
