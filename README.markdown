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
