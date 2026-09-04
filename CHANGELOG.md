# Changelog

Ce fichier distingue les modifications du logiciel des travaux de
documentation. Les éléments marqués « documenté » proviennent des résultats
d'installation et d'exploitation disponibles ; ils ne remplacent pas un audit
du code source.

## Unreleased

### Documentation

- État des lieux de la baseline V10.7.6 avec policy V10.7.4.
- Formalisation des invariants radio et des priorités.
- Architecture cible FT8/FT4 et roadmap du moteur d'opportunité.
- Politique d'exclusion des secrets, journaux, bases et configurations privées.
- Collecteur DigiPi en lecture seule avec contrôle des empreintes V10.7.6.
- Import du code et des 101 tests récupérés depuis la baseline DigiPi.
- Neutralisation du chemin matériel CAT-2 spécifique à l'installation.

### Limitation restante

- L'exemple de configuration public complet reste à produire avant la release.

## 10.7.6 - 2026-09-03

### Documenté comme validé

- Module `v1076_terminal_revisit.py` chargé avec le marqueur
  `terminal-repeat + mandatory-revisit installed`.
- Conservation du QSO jusqu'à la transmission RF réelle du 73 final.
- Surveillance terminale après la fin de cette transmission.
- Nouvelle demande de 73 lorsqu'un nouveau `RRR` ou `RR73` dirigé est reçu
  pendant la surveillance terminale.
- Sémantique de mandatory revisit ajoutée au-dessus de la policy V10.7.4.
- 101 tests réussis et démarrage stable du service observé lors de
  l'installation.

### Limites observées

- Le message MSHV Multi-Answer
  `F4EGM RR73; JA1MLV <CN8NS> -08` n'a pas suivi le chemin terminal normal.
- Le post-QSO hold de 120 secondes démarrait trop tôt. La durée et son origine
  doivent être remplacées par un nombre de slots comptés depuis
  `TERMINAL_COMPLETE`.

## 10.7.4 - 2026-09-03

### Documenté comme validé

- Policy identifiée au démarrage comme
  `proactive backoff + transactional QSY installed`.
- Le backoff concerne les tentatives proactives sans réponse directe.
- La transaction QSY n'est consommée qu'après validation effective d'une
  sélection.
- 101 tests réussis dans l'environnement Python exact du service.
- Contrôle de démarrage avec PID stable après installation.
