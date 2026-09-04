# Audit du code V10.7.6

## Résultat

La baseline a été récupérée du DigiPi le 4 septembre 2026 dans une archive de
revue portant le SHA-256 :

```text
a1619fa7ae3cbc0559c89ae8cb92f6495b1e984077304c4d6556c7fa72cc9507
```

Le manifeste interne est valide. L'ensemble contient 26 modules Python, 9
plugins, 11 fichiers de tests et 101 méthodes de test.

Validation effectuée sur le DigiPi avec
`/var/lib/wavelogstoat/ft8commander/venv/bin/python` :

```text
Ran 101 tests in 0.345s
OK
V10.7.4 self-test: OK
V10.7.6 self-test: OK
```

Le lancement des tests sous Windows n'est pas une validation applicable : le
runtime dépend de l'API POSIX `termios` et de `DXEntity` dans l'environnement du
service. La compilation syntaxique de tous les modules réussit sous Python
3.14.3 sur Windows.

## Architecture réellement présente

Le noyau `ft8ctrl.py` définit seulement trois états QSO : `IDLE`, `ATTEMPT` et
`ENGAGED`. Les évolutions sont installées par couches au démarrage :

1. `install_v60_runtime(Sequencer, QSOState, LOG)` remplace plusieurs méthodes
   du séquenceur ;
2. `v107_policy.install(Sequencer)` ajoute la policy V10.7.4 ;
3. `v1076_terminal_revisit.install(Sequencer)` ajoute la policy V10.7.6.

`TERMINAL_WATCH` n'est donc pas encore un état explicite de la machine : la
V10.7.6 le représente par une structure terminale auxiliaire tout en maintenant
l'état `ENGAGED`. La roadmap demande de rendre cette séparation explicite.

Les composants présents couvrent notamment :

- protocole UDP WSJT-X ;
- base SQLite des candidats par `CALL+BAND` ;
- sélection par plugins ;
- band hopping adaptatif ;
- CAT-2 et planification TXDF pour FTX-1 ;
- mémoire DXCC ;
- synchronisation Wavelog/QRZ ;
- intégrations consultatives PSKReporter MQTT et DX Cluster.

La présence d'un module ne constitue pas une validation opérationnelle. En
particulier, PSKReporter reste à tester et `paho-mqtt` n'est pas déclaré dans les
dépendances de base.

## Défauts et dettes confirmés par le code

### Parsing MSHV Multi-Answer

`process_decode()` découpe le message brut au point-virgule, puis
`parse_segment()` exige deux indicatifs dans chaque segment de réponse. Pour :

```text
F4EGM RR73; JA1MLV <CN8NS> -08
```

le premier segment n'a pas d'émetteur reconnu et ne peut pas produire le
terminal dirigé attendu. Un normaliseur au niveau de la trame complète est
nécessaire.

### Machine d'état empilée

Les policies reposent sur des remplacements de méthodes successifs. Cette
structure a permis des correctifs rapides, mais rend l'ordre d'installation et
les interactions entre versions difficiles à vérifier. Le futur arbitre doit
avoir des états et priorités explicites.

### Temporisations en secondes

Le code contient notamment une grâce terminale de 22 secondes, un watchdog de
75 secondes et plusieurs holds de 90 ou 120 secondes. Il n'existe aucun
`ModeProfile`. Ces valeurs doivent être classées entre délais physiques et
délais de protocole avant l'ajout de FT4.

### Couverture V10.7.x

Les 101 tests sont concentrés dans les fichiers `test_v60_*`. V10.7.4 et
V10.7.6 utilisent encore des self-tests intégrés à leurs modules. Ils passent,
mais doivent devenir des tests de régression indépendants avant une refonte.

### Données externes

Le client PSKReporter est optionnel et désactivé par défaut. Il utilise MQTT et
échoue sans bloquer le moteur local si la dépendance ou le réseau manque. Son
protocole, sa sécurité de transport, ses topics et la qualité des données ne
sont pas encore validés dans cet audit.

## Contrôle de confidentialité

Aucun mot de passe, token, clé API, configuration réseau privée, base SQLite,
ADIF ou journal runtime n'a été importé. Un chemin par défaut contenant le
numéro de série du convertisseur CAT-2 a été remplacé par `/dev/ttyUSB1`. Le
chemin stable réel doit rester dans `ft8ctrl.yaml`, ignoré par Git.

Les appels `F4EGM` présents dans les self-tests et fixtures sont conservés : ils
font partie des scénarios radio déjà publiquement documentés et ne constituent
pas un secret d'authentification.

## Incohérences historiques conservées comme constat

- Le `VERSION` du DigiPi indiquait encore `6.0.0-dev2` alors que V10.7.4 et
  V10.7.6 étaient installées et chargées.
- `README-V6.0.md` décrivait encore le TXDF comme non validé sur le matériel.
- Les anciens scripts de setup peuvent modifier la configuration et redémarrer
  le service ; ils ne sont pas inclus dans la baseline publique avant un audit
  fonctionnel séparé.
