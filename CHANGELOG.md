# Changelog

Ce fichier distingue les modifications du logiciel des travaux de
documentation. Les éléments marqués « documenté » proviennent des résultats
d'installation et d'exploitation disponibles ; ils ne remplacent pas un audit
du code source.

## Unreleased

### Corrigé

- Ajout d'un normaliseur conservateur pour le format MSHV Multi-Answer
  `F4EGM RR73; JA1MLV <CN8NS> -08`.
- Reconstruction du premier échange en ordre reçu destinataire/émetteur
  (`F4EGM CN8NS RR73`) avant passage au parseur existant.
- Utilisation du même normaliseur dans le séquenceur de base, les runtimes V5.5
  et V6, ainsi que la surveillance terminale V10.7.6.
- Ajout de tests unitaires et d'un test d'intégration du parseur.

## 10.7.6 - 2026-09-04

### Publication de la baseline

- Import du code, des plugins et des 101 tests récupérés depuis le DigiPi.
- Vérification des quatre empreintes de référence dans un clone GitHub séparé.
- Exécution réussie des 101 tests et des self-tests V10.7.4/V10.7.6 avec le
  Python du service.
- Ajout d'une configuration d'exemple entièrement fictive.
- Neutralisation du chemin matériel CAT-2 spécifique à l'installation.
- État des lieux, décisions radio, architecture et roadmap versionnés.
- Politique d'exclusion des secrets, journaux, bases et configurations privées.

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
