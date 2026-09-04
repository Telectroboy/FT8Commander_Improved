# Roadmap

La roadmap sépare récupération de la baseline, corrections déterministes et
moteur adaptatif. Aucun élément futur n'est présenté comme déjà implémenté.

## P0 — Rendre V10.7.6 reconstructible

- [ ] Exporter du DigiPi les quatre fichiers portant les SHA-256 documentés.
- [ ] Exporter la suite exacte des 101 tests.
- [ ] Retirer secrets, configurations privées, journaux, ADIF, bases SQLite,
  sauvegardes et états runtime.
- [ ] Rejouer les tests avec le Python exact du service.
- [ ] Auditer la structure et les dépendances réellement importées.
- [ ] Ajouter un exemple de configuration entièrement fictif.
- [ ] Créer un commit d'import indépendant.
- [ ] Créer le tag annoté `v10.7.6` et la release seulement après correspondance
  des empreintes et des tests.

## P1 — Corriger les règles déterministes

- [ ] Introduire `DecodeNormalizer`.
- [ ] Prendre en charge les messages MSHV Multi-Answer, dont
  `F4EGM RR73; JA1MLV <CN8NS> -08`.
- [ ] Séparer explicitement `ENGAGED_ACTIVE`, `TERMINAL_WATCH` et
  `TERMINAL_COMPLETE`.
- [ ] Rendre `PENDING_DIRECT` persistant sur plusieurs slots.
- [ ] Servir tous les pending directs après la fin du QSO et avant le proactif.
- [ ] Répondre à tout vrai `DIRECT_TO_ME`, doublon `CALL+BAND` compris.
- [ ] Bloquer `CALL+BAND` déjà travaillé uniquement dans les chemins proactifs,
  y compris par une vérification juste avant émission.
- [ ] Implémenter les états `BUSY`, `TERMINATING`, `OPENING`, `FREE_CQ` et
  `DIRECT_TO_ME`.
- [ ] Préserver la sémantique de mandatory revisit documentée.
- [ ] Démarrer le post-QSO hold à `TERMINAL_COMPLETE` et le compter en slots.
- [ ] Résoudre et tester les points ouverts de `TERMINAL_WATCH`.

## P2 — Abstraction du protocole

- [ ] Créer `ModeProfile` et déplacer les temporisations dépendantes du mode.
- [ ] Remplacer les secondes fixes par slots/opportunités RX/TX dans les règles.
- [ ] Conserver les délais physiques CAT/série en secondes.
- [ ] Valider le profil FT8 sans changer le comportement intentionnel.
- [ ] Ajouter et tester un profil FT4 avant d'autoriser FT4 en émission.

## P3 — Mémoire et observation

- [ ] Charger et mettre à jour la mémoire stricte `CALL+BAND`.
- [ ] Conserver un historique SNR par `CALL+BAND` avec horodatage et type de
  message.
- [ ] Mesurer activité, CQ, correspondants uniques, cadence et pression pile-up.
- [ ] Représenter le contexte de rareté et de DXpedition avec provenance,
  fraîcheur et niveau de confiance.
- [ ] Étudier les données de propagation externes sans les placer dans la boucle
  critique.
- [ ] Tester le mode PSKReporter ; sa spécification fonctionnelle reste à
  retrouver ou définir.

## P4 — Shadow opportunity engine

- [ ] Journaliser candidats, observations, score, décision proposée et raisons.
- [ ] Ne commander aucune émission en shadow mode.
- [ ] Comparer sur plusieurs sessions : décision V10.7.6, décision humaine et
  décision shadow.
- [ ] Transformer le backoff futur en forte pénalité plutôt qu'en blocage absolu,
  après définition et validation des paramètres.
- [ ] Définir un budget TX adaptatif uniquement après analyse des données.
- [ ] Prévoir un déploiement progressif et un retour instantané à la policy
  validée.

## P5 — Awards : États-Unis

États signalés comme manquants dans le contexte projet ; cette liste devra être
revalidée contre le journal opérateur avant d'influencer une décision :

- [ ] Montana
- [ ] Idaho
- [ ] Wyoming
- [ ] Nevada
- [ ] Utah
- [ ] New Mexico
- [ ] Kansas
- [ ] Hawaii

## Critères permanents

- [ ] Toute correction de protocole possède un test de régression.
- [ ] Aucun invariant radio n'est remplacé par un score.
- [ ] Aucun secret ni donnée radio privée n'est versionné.
- [ ] Les releases correspondent à un commit reconstructible et testé.
- [ ] Les décisions nouvelles ou modifiées sont enregistrées dans
  `docs/DECISIONS.md`.
