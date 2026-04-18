# TripleAgent

**TripleAgent** est un moteur expérimental d’agents animés inspiré de **Microsoft Agent**, orienté vers l’exploration, l’analyse et le rendu moderne de fichiers **ACS** avec une interface interactive en Python.

Le projet a désormais dépassé le simple cas **Clippy** et prend en charge une compatibilité plus robuste avec plusieurs agents, notamment **Merlin**.  
Il combine :

- parsing ACS,
- rendu palette → RGBA explicite,
- overlays de bouche,
- bulles de dialogue,
- modes fenêtre / overlay,
- machine d’états interactive,
- commandes de diagnostic.

L’objectif n’est pas seulement de “jouer une animation”, mais de construire un **vrai runtime d’agent de bureau**, extensible et observable.

---

## État actuel du projet

Le projet a atteint un stade nettement plus stable qu’au début.

### Ce qui fonctionne maintenant

- chargement et parsing de fichiers **ACS**
- mode interactif en ligne de commande
- affichage d’animations
- bulles de dialogue
- rendu palette **générique** (plus seulement “compatible Clippy par hasard”)
- décodage RGBA explicite depuis la palette ACS
- gestion correcte du **transparent color index**
- décodage tenant compte du **stride / row padding** DIB
- support des **mouth overlays**
- support des **mouth-bank frames** pour des agents comme **Merlin**
- maintien de la dernière frame visible pendant les transitions visuellement vides
- amélioration du wrapping de bulle basée sur la **largeur réelle mesurée**
- mode `overlay on/off` plus cohérent
- fenêtre Tk cachée au démarrage tant qu’aucune vraie frame visible n’existe
- commandes de debug et d’inspection du rendu

### Ce qui a été corrigé récemment

- tempêtes d’événements dans la boucle interactive
- boucle idle bavarde / polling trop fréquent
- réapparition spontanée de textes comme **“Bonjour !”** ou **“Je t’écoute”**
- mauvais comportement topmost en `overlay off`
- bascule implicite involontaire de `overlay off` vers `overlay on`
- rendu couleur faux dû à un pipeline palette/transparence trop fragile
- disparition de Merlin entre deux animations à cause de frames de transition visuellement vides
- bulle vide affichée au démarrage avant tout rendu

---

## Philosophie du projet

TripleAgent vise à devenir un **runtime générique Microsoft Agent**, pas un lecteur codé “à la forme de Clippy”.

Cela implique :

- respecter la structure réelle des fichiers ACS,
- séparer parsing, rendu, animation, bulle et FSM,
- éviter les hacks spécifiques à un seul agent,
- documenter les différences structurelles entre agents,
- fournir des outils de diagnostic pour comprendre les compatibilités et écarts.

---

## Compatibilité actuelle

### Clippy
Clippy reste un excellent agent de validation, mais n’est plus considéré comme la seule référence implicite.

TripleAgent gère :

- animations classiques,
- transitions fluides,
- bulle,
- modes overlay / fenêtre,
- inspection de rendu.

### Merlin
Merlin est devenu un vrai deuxième cas de compatibilité, et a permis de corriger plusieurs hypothèses trop “Clippy-spécifiques”.

TripleAgent gère maintenant pour Merlin :

- rendu palette correct,
- support des frames de bouche réutilisables,
- overlays de bouche animés,
- maintien visuel entre frames de transition vides,
- inspection détaillée des frames et overlays.

---

## Architecture générale

Le projet s’articule autour de plusieurs briques.

### 1. Parser ACS
Le parser lit les fichiers ACS et extrait :

- animations,
- frames,
- images,
- overlays,
- audio,
- métadonnées de bulle,
- palette,
- informations de transparence.

Le parser n’est plus le principal suspect quand un problème de rendu apparaît : le pipeline de composition a été renforcé pour exploiter correctement les données décodées.

### 2. Rendu
Le moteur de rendu :

- reconstruit explicitement les pixels **RGBA** depuis la palette ACS,
- applique l’index de transparence,
- gère correctement le stride réel des lignes,
- compose les frames et overlays,
- peut détecter les frames visuellement vides.

Le profil de rendu courant est exposé dans les diagnostics comme :

`generic-rgba-palette`

### 3. Machine d’états
La FSM pilote :

- présence,
- comportement,
- écoute,
- parole,
- transitions d’animation,
- événements utilisateur.

Des bugs de réinjection ou de texte implicite ont été corrigés pour garder un comportement plus prévisible.

### 4. Interface Tk
L’interface graphique prend en charge :

- mode fenêtre normal,
- mode overlay,
- topmost,
- affichage conditionnel seulement quand une vraie frame est prête,
- interaction par clic,
- menu contextuel.

### 5. Bulles de dialogue
Le système de bulle :

- lit les métadonnées ACS,
- mesure la largeur réelle du texte,
- wrappe selon la largeur réellement rendue,
- distingue mieux entre métadonnées ACS et capacité pratique d’affichage.

---

## Installation

### Prérequis

- Python 3.10+ recommandé
- Tkinter disponible
- Pillow

Selon l’état de ton environnement, il peut aussi être utile d’avoir :

- `py` launcher sur Windows
- Git

### Installation simple

```bash
git clone https://github.com/CozmoCyke/tripleagent.git
cd tripleagent
pip install -r requirements.txt

---

## Lancement

### Clippy

```bash
python agentpy_app.py CLIPPIT.ACS --interactive
```

### Merlin

```bash
python agentpy_app.py Merlin.acs --interactive
```

Ou selon l’organisation locale de tes fichiers :

```bash
python agentpy_app.py Merlin --interactive
```

---

## Mode interactif

Au démarrage :

```text
Interactive mode. Type 'help' to see available commands.
```

Le mode interactif permet de jouer des animations, parler, inspecter le rendu, analyser les bulles, etc.

---

## Commandes utiles

### Afficher une animation

```text
show Wave
```

### Dire du texte

```text
say Bonjour
```

### Parole

```text
speak bonjour
```

### Écoute

```text
listen
listen on
listen off
listen status
unlisten
```

### Informations de bulle

```text
balloon-info
```

### Lire une timeline

```text
timeline read
```

### Diagnostic rendu

```text
render-dump Wave 12
```

### Comparaison avec un autre agent

```text
render-dump Wave 12 .\Merlin.acs
```

### Diagnostic des bouches

```text
mouth-dump Read 12
```

Ou avec export :

```text
mouth-dump Read 12 ./mouth_debug
```

---

## Diagnostics disponibles

TripleAgent dispose maintenant de plusieurs outils d’inspection.

### `timeline <animation>`

Permet d’observer :

* frames,
* durée,
* audio,
* overlays,
* branches,
* sorties.

Très utile pour détecter :

* frames de bouche,
* frames techniques,
* transitions atypiques.

### `render-dump`

Affiche notamment :

* profil de rendu,
* taille image,
* taille palette,
* `transparent_color_index`,
* stride attendu/réel/utilisé,
* padding par ligne,
* source mouth-bank si présente.

C’est l’outil principal pour comparer deux agents comme Clippy et Merlin.

### `mouth-dump`

Permet d’inspecter les overlays de bouche :

* type,
* image source,
* offsets,
* dimensions,
* présence de région,
* export éventuel des variantes.

### `balloon-info`

Distingue désormais :

* `num_lines (ACS metadata)`
* `chars_per_line (ACS metadata)`
* largeur pratique mesurée
* largeur moyenne de caractère mesurée
* capacité pratique approximative

---

## Comportement de fenêtre et overlay

TripleAgent distingue maintenant proprement deux modes :

### Overlay on

* affichage agent sans fenêtre visible classique
* comportement topmost
* plus “desktop assistant”

### Overlay off

* fenêtre normale visible
* reste au-dessus si tel est le comportement voulu
* ne doit plus basculer automatiquement en overlay on sur clic

### Démarrage

Au lancement :

* la fenêtre Tk démarre cachée (`withdraw`)
* aucune fenêtre vide n’est montrée
* la fenêtre n’apparaît que lorsqu’une vraie frame visible est prête

---

## Parole et bouche

### Merlin

Merlin exploite des **mouth-bank frames** réutilisables.
Le runtime sait maintenant :

* chercher une source d’overlays de bouche,
* la réutiliser si la frame courante n’en contient pas,
* animer les bouches sans dépendre d’une hypothèse “frame locale uniquement”.

### Clippy

Clippy ne suit pas forcément le même modèle que Merlin pour la bouche.
Le moteur sait désormais mieux distinguer :

* agent avec mouth overlays réutilisables,
* agent sans banque de bouche explicite,
* animation native versus composition dynamique.

---

## Rendu palette et transparence

L’ancien pipeline de rendu dépendait trop du comportement implicite de conversion palette → RGBA.
Le pipeline actuel :

* reconstruit chaque pixel explicitement depuis la palette ACS,
* applique la transparence à partir des métadonnées ACS,
* gère correctement les lignes DIB paddées,
* réduit les comportements accidentellement “bons seulement avec Clippy”.

C’est l’une des plus grosses avancées du projet.

---

## Gestion des transitions vides

Certains agents, comme Merlin, utilisent des frames techniquement valides mais visuellement vides pendant les transitions.

Avant :

* ces frames remplaçaient la dernière image visible,
* l’agent disparaissait brièvement.

Maintenant :

* les frames vides continuent à faire avancer l’état logique,
* mais n’écrasent plus la dernière frame réellement visible,
* ce qui supprime le clignotement.

---

## Bulle et wrapping

Les métadonnées ACS de bulle ne doivent pas être interprétées comme une garantie stricte de capacité visible.

Le moteur s’appuie désormais sur :

* mesure réelle en pixels,
* largeur pratique de texte,
* zone utile interne plus réaliste.

Cela évite qu’un `chars_per_line: 32` se comporte visuellement comme 16 caractères seulement sans explication.

---

## Débogage

Le projet dispose de logs ciblés, activables en debug, pour éviter le bruit en mode normal.

Exemples de traces debug possibles :

* source d’événement FSM
* transitions de comportement
* changements de présence
* changement de mode overlay
* topmost
* source de texte de bulle
* sélection de source mouth-bank

L’objectif est d’obtenir un runtime **observable**, sans rendre le mode normal bavard.

---

## Limitations actuelles

Même si le projet est devenu beaucoup plus solide, il reste expérimental.

Quelques limites possibles :

* compatibilité encore incomplète selon les agents ACS
* police de bulle dépendante de la résolution/fallback système
* coût de parsing encore élevé sur certains fichiers
* certaines animations ou comportements natifs peuvent encore révéler des cas spéciaux
* tous les agents Microsoft Agent n’ont pas encore été validés au même niveau

---

## Pistes futures

Voici des directions naturelles pour la suite.

### Compatibilité

* valider Genie, Peedy, Robby, etc.
* cataloguer les différences structurelles entre agents

### Performance

* cache plus agressif
* pré-décodage
* module natif éventuel pour certaines parties lourdes

### Rendu

* export d’images de debug depuis `render-dump`
* comparaison visuelle automatisée entre agents
* meilleure gestion des polices système/fallback

### FSM et interaction

* profils comportementaux spécifiques par agent
* écoute / parole plus naturelles
* gestes contextuels
* meilleure séparation entre personnalité et runtime brut

### Outils

* meilleure inspection CLI
* diagnostics persistants
* documentation des structures ACS rencontrées

---

## Exemple de vision du projet

TripleAgent n’est pas seulement un “lecteur de Clippy”.

Le projet devient progressivement :

* un **moteur de compatibilité ACS**,
* un **laboratoire d’archéologie logicielle**,
* un **runtime moderne pour assistants animés rétro**,
* et potentiellement une base pour un assistant interactif beaucoup plus vivant.

---

## Développement

Exemple de vérification rapide :

```bash
py -m py_compile agentpy_app.py clippy_state_machine.py speech_controller.py agentpy_parser.py
```

---

## Dépôt

GitHub : `CozmoCyke/tripleagent`
