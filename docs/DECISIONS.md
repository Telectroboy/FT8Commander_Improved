# Décisions de conception

Ce document est normatif pour les règles déjà décidées. Les valeurs ou arbitrages
non décidés sont listés séparément afin de ne pas les inventer.

## D-001 — Orientation des indicatifs reçus

**Statut : accepté**

Pour un message FT8 reçu de forme conventionnelle contenant deux indicatifs :

```text
DESTINATAIRE EMETTEUR CONTENU
```

le premier indicatif est le destinataire et le second est l'émetteur. Le parseur
ne doit pas inverser ces rôles.

## D-002 — Normalisation MSHV Multi-Answer

**Statut : accepté**

Une trame Multi-Answer peut contenir plusieurs échanges logiques. L'exemple :

```text
F4EGM RR73; JA1MLV <CN8NS> -08
```

doit au minimum produire un terminal `RR73` envoyé par `CN8NS` à `F4EGM`, ainsi
qu'un second échange envoyé par `CN8NS` à `JA1MLV` avec le report `-08`. Toute
décision QSO doit consommer les événements normalisés et non la chaîne brute.

Référence de syntaxe vérifiée :
[journal MSHV](https://sourceforge.net/projects/mshv/files/README.txt/download).

## D-003 — `ENGAGED_ACTIVE` est inviolable

**Statut : accepté**

Un QSO réellement engagé est toujours terminé proprement. Aucun CQ, candidat
proactif, score, rareté ou nouvel appel direct ne l'interrompt.

Un `DIRECT_TO_ME` reçu pendant cet état est enregistré dans `PENDING_DIRECT`
avec les informations disponibles : indicatif, bande, DF, SNR, slot, message
brut et ordre d'arrivée. La mémoire doit survivre à plusieurs slots.

## D-004 — Répondre aux appels directs

**Statut : accepté**

Un vrai `DIRECT_TO_ME` reçoit une réponse. Les filtres d'award, le backoff, le
score SNR, l'état antérieur de la station et un doublon `CALL+BAND` ne peuvent
pas supprimer cette réponse.

Si un QSO est engagé, « répondre » signifie mémoriser l'appel puis le traiter
après `TERMINAL_COMPLETE`, pas abandonner le QSO courant.

## D-005 — Portée de `CALL+BAND` déjà travaillé

**Statut : accepté**

```text
PROACTIVE + CALL+BAND travaillé  => ne pas appeler
DIRECT_TO_ME + CALL+BAND travaillé => répondre
```

La mémoire `CALL+BAND` est distincte d'une mémoire `DXCC+BAND` utilisée pour les
awards.

## D-006 — File des appels entrants

**Statut : accepté**

Après `TERMINAL_COMPLETE`, les `PENDING_DIRECT` sont traités avant toute action
proactive. Toutes les stations reçues sont conservées. Une station observée
`BUSY` reste en mémoire jusqu'à une nouvelle opportunité ou à l'expiration de
la mémoire.

## D-007 — États observés d'une station

**Statut : accepté**

- `BUSY` : la station est engagée avec un autre correspondant.
- `TERMINATING` : elle arrive à `RRR`, `RR73` ou `73` avec ce correspondant.
- `OPENING` : elle vient de terminer ; c'est une forte opportunité, pas une
  certitude de disponibilité au slot suivant.
- `FREE_CQ` : elle lance un CQ et cherche explicitement un correspondant.
- `DIRECT_TO_ME` : elle nous appelle explicitement.

Ces états décrivent une observation récente. `PENDING_DIRECT` est une mémoire
d'intention, pas un sixième état de disponibilité.

## D-008 — Mandatory revisit

**Statut : accepté**

- un CQ ou une sélection proactive ne peut pas l'annuler ;
- un QSO `ENGAGED_ACTIVE` la diffère sans la consommer ;
- un appel direct l'annule ;
- une intervention ou un passage manuel l'annule.

## D-009 — `TERMINAL_WATCH`

**Statut : accepté, arbitrage partiellement ouvert**

Après l'émission réelle du 73 final, le dernier correspondant reste surveillé.
Chaque nouveau `RRR` ou `RR73` dirigé et postérieur à un 73 terminé demande une
nouvelle répétition contrôlée du 73. Un CQ ou une sélection proactive ne peut
pas annuler cette surveillance.

Le traitement exact d'un nouvel appel direct pendant `TERMINAL_WATCH` reste à
spécifier ; voir « Points ouverts ».

## D-010 — Post-QSO hold en slots

**Statut : accepté**

Le post-QSO hold commence à `TERMINAL_COMPLETE`, pas à l'événement de log QSO.
Il est exprimé en nombre de slots ou d'opportunités du mode. La valeur exacte
n'est pas encore décidée.

## D-011 — Abstraction `ModeProfile`

**Statut : accepté pour la roadmap**

Les délais dépendants du protocole utilisent des unités logiques : slot,
opportunité RX, opportunité TX, cycle et période du mode. Les secondes restent
réservées aux délais physiques indépendants du mode, par exemple CAT, série ou
stabilisation du poste.

## D-012 — Observation avant contrôle adaptatif

**Statut : accepté pour la roadmap**

Le futur moteur d'opportunité est d'abord exécuté en shadow mode. Il observe et
explique ses choix sans piloter l'émission. L'historique SNR `CALL+BAND`, le
modèle d'activité/pile-up, la rareté/DXpedition et la propagation externe sont
des entrées futures, pas des fonctions déclarées présentes en V10.7.6.

## D-013 — Données publiques

**Statut : accepté**

Aucun secret, journal runtime, ADIF personnel, base SQLite, configuration privée
ou sauvegarde d'installation ne doit être versionné.

## Points ouverts

- Nombre de 73 autorisés pendant `TERMINAL_WATCH` et sens exact de « deux
  tentatives ».
- Arbitrage entre `TERMINAL_WATCH` et un direct rare ou prioritaire.
- Nombre de slots du post-QSO hold et durée de vie de `PENDING_DIRECT`.
- Ordre exact entre plusieurs `PENDING_DIRECT` simultanés ; aucun tie-break n'est
  fixé ici.
- Paramètres du backoff futur lorsqu'il devient une forte pénalité plutôt qu'un
  mur.
- Spécification du mode PSKReporter à tester.
- Sources, format, fréquence de rafraîchissement et confiance des données de
  propagation et de rareté.
