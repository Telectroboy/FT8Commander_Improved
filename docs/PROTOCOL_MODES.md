# Protocoles et profils de mode

## Événement décodé normalisé

La logique métier doit recevoir un objet conceptuel de cette forme :

```text
DecodedEvent
  raw_message
  sender
  recipient
  payload
  snr
  delta_frequency
  dial_frequency
  band
  mode
  rx_slot
  received_at_monotonic
  is_direct_to_me
  is_terminal
```

Un message brut peut produire plusieurs `DecodedEvent`.

## Message conventionnel

```text
F4EGM CN8NS -02
```

Normalisation attendue :

```text
recipient=F4EGM
sender=CN8NS
payload=-02
is_direct_to_me=true
```

La règle est : premier indicatif destinataire, second indicatif émetteur.

## MSHV Multi-Answer

```text
F4EGM RR73; JA1MLV <CN8NS> -08
```

Événements logiques attendus :

```text
sender=CN8NS recipient=F4EGM  payload=RR73 terminal=true
sender=CN8NS recipient=JA1MLV payload=-08  terminal=false
```

Le premier segment ne doit pas être rejeté parce qu'il ne ressemble pas à un
message conventionnel à deux indicatifs. L'indicatif émetteur entre chevrons du
segment suivant fournit le contexte commun de cette trame spéciale.

La syntaxe `A2AA RR73; B2BB <C2CC> +05` est mentionnée par le projet MSHV dans
son [README publié sur SourceForge](https://sourceforge.net/projects/mshv/files/README.txt/download).

## Machine d'état QSO

```text
IDLE
  | sélection/proposition
  v
ATTEMPT
  | réponse dirigée et progression
  v
ENGAGED_ACTIVE
  | terminal distant reçu
  | 73 final réellement émis
  v
TERMINAL_WATCH
  | nouveau RRR/RR73 -> répéter 73, rester en surveillance
  | confirmation ou fin de fenêtre
  v
TERMINAL_COMPLETE
  | démarrer le post-QSO hold en slots
  | servir PENDING_DIRECT
  v
IDLE / prochain direct
```

`WSLogged` est une observation utile, mais ne doit pas à lui seul faire passer
le QSO à `TERMINAL_COMPLETE` avant la fin RF et la surveillance requise.

## Profil de mode

Le futur contrat peut exposer les concepts suivants sans imposer ici leurs
valeurs :

```text
ModeProfile
  name
  slot_duration
  rx_decode_completion_offset
  tx_opportunity
  remote_reply_opportunity
  terminal_watch_slots
  post_qso_hold_slots
  pending_direct_ttl_slots
  revisit_delay_slots
```

FT8 et FT4 fourniront chacun leur profil. Une policy demande « prochain slot TX
compatible » ou « N opportunités RX » ; elle ne code pas une durée FT8 en
secondes.

## Tests de protocole obligatoires

### Orientation et parsing

- message conventionnel : destinataire puis émetteur ;
- CQ standard et variantes déjà prises en charge par la baseline importée ;
- MSHV Multi-Answer avec `RR73` dans le premier segment ;
- indicatif émetteur entre chevrons ;
- plusieurs événements issus d'une seule trame ;
- message malformé conservé pour diagnostic, sans décision radio.

### Priorités

- un direct reçu en `ENGAGED_ACTIVE` ne préempte pas le QSO ;
- ce direct est encore présent après plusieurs slots ;
- après `TERMINAL_COMPLETE`, il passe avant un CQ et un candidat proactif ;
- un doublon `CALL+BAND` bloque le proactif mais pas le direct ;
- une mandatory revisit survit à un CQ, est différée par `ENGAGED_ACTIVE` et est
  annulée par un direct ou une action manuelle.

### Terminal

- l'événement de log ne termine pas prématurément le QSO ;
- le 73 final doit être observé en RF avant `TERMINAL_WATCH` ;
- un nouveau `RRR/RR73` reçu après un 73 terminé demande une seule nouvelle
  répétition contrôlée ;
- un message MSHV terminal suit le même chemin qu'un terminal conventionnel ;
- le post-QSO hold démarre à `TERMINAL_COMPLETE` et compte des slots.

### Multi-mode

- la même suite de transitions s'exécute avec un profil FT8 factice et un profil
  FT4 factice ;
- aucune policy ne dépend directement d'une constante de durée propre à FT8 ;
- les timeouts CAT restent indépendants du profil radio.
