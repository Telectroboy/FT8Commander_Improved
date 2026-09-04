# Baseline validée V10.7.6

## Portée de l'état des lieux

La version opérationnelle connue est **V10.7.6 avec la policy V10.7.4**. Les
éléments ci-dessous sont vérifiables à partir de la sortie d'installation et du
journal d'exploitation conservés le 3 septembre 2026.

Le code source et la suite de tests correspondants ne sont pas disponibles dans
le dépôt au moment de cet état des lieux. Il est donc impossible d'auditer la
mise en œuvre, de relancer les 101 tests ou de reconstruire cette version depuis
GitHub.

## Inventaire observé sur le DigiPi

| Fichier | SHA-256 observé |
|---|---|
| `ft8ctrl.py` | `d2ecc5b4aba7b3671863e527f4bc03717366054de0607ff7302d2ec7dd65314a` |
| `v60_runtime.py` | `af75a365edd32688793ccdf0557e5c57ffe0fea4c5b4296b0c14338397f06b0a` |
| `v107_policy.py` | `3ea711332e5b0dfbd8b66b31829eae51fb45be8087257e02786d75f65a34e11c` |
| `v1076_terminal_revisit.py` | `73fa3cacb4cac10ce56d36dc86f915af3e63701080aff29e1af403399c694b21` |

Résultats observés :

- `Ran 101 tests ... OK` ;
- service `ft8commander.service` actif avec un PID stable après le smoke test ;
- Python du service :
  `/var/lib/wavelogstoat/ft8commander/venv/bin/python` ;
- entrée principale : `/home/pi/FT8Commander/ft8ctrl.py` ;
- marqueur V10.7.4 : `proactive backoff + transactional QSY installed` ;
- marqueur V10.7.6 : `terminal-repeat + mandatory-revisit installed`.

## Comportement validé en exploitation

Sur un QSO standard avec VK3GSX, le journal a montré la séquence suivante :

1. passage de `ATTEMPT` à `ENGAGED` après une réponse dirigée ;
2. détection du terminal `RRR` ;
3. observation de la transmission RF réelle du 73 final ;
4. refus d'un effacement prématuré causé par l'événement de log QSO ;
5. écoute terminale après la fin RF ;
6. `TERMINAL_COMPLETE`, puis retour à l'état inactif.

Ce résultat valide ce scénario observé. Il ne démontre pas que toutes les
variantes de message ou toutes les courses d'événements sont couvertes.

## Défaut reproductible identifié

Le message reçu :

```text
F4EGM RR73; JA1MLV <CN8NS> -08
```

est un message spécial MSHV Multi-Answer. Le QSO CN8NS a été loggé, mais les
marqueurs normaux de terminal tail et de transmission du 73 final n'ont pas été
observés. Le parseur doit produire au moins l'événement logique :

```text
sender=CN8NS
recipient=F4EGM
payload=RR73
terminal=true
```

La syntaxe spéciale est également attestée dans le journal des versions MSHV :
[README MSHV sur SourceForge](https://sourceforge.net/projects/mshv/files/README.txt/download).

## Conditions avant création du tag `v10.7.6`

1. Copier les quatre fichiers portant exactement les empreintes ci-dessus et
   la suite de tests depuis le DigiPi vers un répertoire temporaire.
2. Exclure et contrôler les secrets, configurations privées, journaux, ADIF,
   bases SQLite, sauvegardes et états runtime.
3. Exécuter la suite dans l'environnement documenté.
4. Comparer les nouvelles empreintes aux valeurs de cette page.
5. Importer le snapshot sans réécriture fonctionnelle.
6. Créer un commit d'import distinct, puis seulement le tag annoté `v10.7.6` et
   la release correspondante.

Tant que ces conditions ne sont pas remplies, `VERSION` désigne la baseline
documentée et non une release reconstructible depuis ce dépôt.
