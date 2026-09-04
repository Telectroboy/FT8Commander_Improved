# Architecture

## État actuel vérifié

Le runtime V10.7.6 récupéré du DigiPi est composé notamment de :

- `ft8ctrl.py`, entrée principale du service ;
- `v60_runtime.py`, couche runtime héritée ;
- `v107_policy.py`, policy V10.7.4 de backoff proactif et QSY transactionnel ;
- `v1076_terminal_revisit.py`, gestion terminale et mandatory revisit ;
- 26 modules Python et 11 fichiers contenant 101 tests.

Le noyau expose `IDLE`, `ATTEMPT` et `ENGAGED`. Les runtimes V6, V10.7.4 et
V10.7.6 sont installés successivement en remplaçant des méthodes du séquenceur.
`TERMINAL_WATCH` est actuellement une structure auxiliaire, pas encore un état
explicite. L'audit détaillé est dans [AUDIT_V10.7.6.md](AUDIT_V10.7.6.md).

Les dépendances de base déclarées sont `PyYAML`, `DXEntity` et `tabulate`.
SQLite appartient à la bibliothèque standard Python. `paho-mqtt` est une
dépendance optionnelle non encore déclarée pour le module PSKReporter.

## Architecture cible

```text
WSJT-X UDP ----> DecodeNormalizer ----> Observation/Memory
                                          |  - worked CALL+BAND
Log/ADIF -------------------------------->|  - SNR CALL+BAND
PSKReporter (à tester) ------------------>|  - état des stations
Propagation externe -------------------->|  - activité/pile-up
Rareté/DXpedition ---------------------->|  - pending directs
                                          v
                                  Opportunity Engine
                                   |              |
                              shadow decision   active decision
                                          |
                                     QSO Arbiter
                                          |
                                ModeProfile FT8/FT4
                                          |
                                  WSJT-X / CAT radio
```

### `DecodeNormalizer`

Transforme chaque texte décodé en un ou plusieurs événements structurés avant
toute logique de QSO. Il porte la règle destinataire/émetteur et la prise en
charge MSHV Multi-Answer. Le texte brut reste attaché à l'événement pour
diagnostic.

### `Observation/Memory`

Conserve séparément :

- les QSO travaillés par `CALL+BAND` ;
- l'historique SNR par `CALL+BAND` ;
- les appels directs en attente ;
- l'état récent d'une station ;
- les signaux d'activité et de pile-up ;
- les contextes externes de propagation, rareté et DXpedition.

Les données externes enrichissent la décision mais ne doivent pas être
nécessaires à la boucle radio critique.

### `Opportunity Engine`

Classe les opportunités sans violer les invariants radio. Sa première version
doit fonctionner en shadow mode : elle enregistre sa décision et ses raisons,
sans commander la radio. Les paramètres de score, de backoff et de budget TX ne
sont pas encore spécifiés.

### `QSO Arbiter`

Applique les priorités non négociables :

```text
ENGAGED_ACTIVE courant
    > appels PENDING_DIRECT après TERMINAL_COMPLETE
    > mandatory revisit encore valide
    > sélection proactive
```

Un appel direct annule une mandatory revisit qui n'a pas encore commencé. Il ne
préempte pas un QSO `ENGAGED_ACTIVE` : il est mémorisé.

### `ModeProfile`

Fournit les durées physiques et les conversions entre slots, opportunités RX/TX
et temps monotone. Le moteur de décision ne doit pas embarquer d'hypothèse
fixe propre à FT8. FT4 sera ajouté comme second profil après validation de cette
séparation.

## Frontières de sécurité et de confidentialité

- Le contrôle radio et les fournisseurs Internet doivent rester séparés.
- Une indisponibilité Internet ne doit pas corrompre l'état QSO.
- Les timestamps de décision doivent utiliser une horloge monotone lorsque
  l'heure civile n'est pas requise.
- Les secrets et données personnelles restent hors du dépôt.
- Les fixtures publiques doivent être minimales, synthétiques ou anonymisées.
- Toute évolution de l'arbitre doit disposer de tests de non-régression avant
  activation de l'émission.
