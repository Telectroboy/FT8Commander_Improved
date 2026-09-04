# FT8Commander Improved

FT8Commander Improved est une évolution de
[FT8Commander de 0x9900](https://github.com/0x9900/FT8Commander). Le projet vise
à automatiser des QSO FT8 avec WSJT-X tout en conservant des règles radio
explicites, testables et indépendantes du moteur de sélection des cibles.

> [!CAUTION]
> Ce logiciel peut commander une émission radio. Toute version destinée à être
> exécutée doit être testée hors émission puis sous supervision, conformément à
> la réglementation et aux conditions de la licence de l'opérateur.

## État du dépôt

La baseline opérationnelle est **V10.7.6 avec la policy V10.7.4**. Son code et
ses tests ont été récupérés du DigiPi, les quatre empreintes de référence ont
été confirmées et les 101 tests ainsi que les deux self-tests V10.7.x passent
dans l'environnement Python du service.

Une valeur matérielle spécifique au poste a été neutralisée avant l'import
public sans modifier les quatre fichiers portant les empreintes de référence.
Les contrôles et limites sont consignés dans
[docs/BASELINE_V10.7.6.md](docs/BASELINE_V10.7.6.md) et
[docs/AUDIT_V10.7.6.md](docs/AUDIT_V10.7.6.md).

## Invariants radio

- Dans un message FT8 reçu de forme conventionnelle, le premier indicatif est
  le destinataire et le second est l'émetteur.
- Les messages MSHV Multi-Answer doivent être normalisés avant toute décision.
- Un QSO `ENGAGED_ACTIVE` n'est jamais abandonné au profit d'une autre cible.
- Un appel reçu pendant ce QSO est conservé dans `PENDING_DIRECT`.
- Après `TERMINAL_COMPLETE`, les appels directs en attente passent avant toute
  chasse proactive.
- Un vrai `DIRECT_TO_ME` reçoit une réponse, même si `CALL+BAND` est déjà
  travaillé. Le doublon bloque uniquement une sélection proactive.
- Une mandatory revisit ne peut pas être annulée par un CQ ou une action
  proactive. Elle est différée par un QSO engagé et annulée par un appel direct
  ou un passage manuel.
- Les temporisations dépendantes du mode doivent être exprimées en slots ou en
  opportunités de protocole, pas en secondes fixes.

Les définitions normatives et les points encore ouverts sont dans
[docs/DECISIONS.md](docs/DECISIONS.md) et
[docs/PROTOCOL_MODES.md](docs/PROTOCOL_MODES.md).

## Documentation

- [Baseline V10.7.6](docs/BASELINE_V10.7.6.md)
- [Audit du code V10.7.6](docs/AUDIT_V10.7.6.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Décisions](docs/DECISIONS.md)
- [Protocoles et modes](docs/PROTOCOL_MODES.md)
- [Roadmap](docs/ROADMAP.md)
- [Historique](CHANGELOG.md)
- [Collecteur de la baseline DigiPi](scripts/README.md)

## Données exclues

Le dépôt public ne doit contenir aucun token, mot de passe, clé API,
configuration réseau privée, journal d'exploitation, journal WSJT-X, ADIF
personnel, base SQLite, sauvegarde d'installation ou état runtime. Les exemples
de configuration futurs devront utiliser des valeurs fictives.

## Origine et licence

Le projet est basé sur le dépôt public de Fred Cirera (`0x9900`), distribué sous
licence BSD 3-Clause. La licence et l'attribution d'origine sont conservées dans
[LICENSE](LICENSE).


