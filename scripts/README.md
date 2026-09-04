# Scripts de migration

## Collecter la baseline V10.7.6

`collect-v10.7.6.sh` est à exécuter directement sur le DigiPi. Il ne modifie pas
l'installation et n'arrête pas le service. Il :

1. vérifie les quatre empreintes SHA-256 documentées ;
2. collecte les modules Python, les tests et les métadonnées de build autorisées ;
3. exclut par construction configurations runtime, bases, ADIF, logs, clés et
   sauvegardes ;
4. produit `/home/pi/ft8commander-v10.7.6-review.tar.gz` et son empreinte.

Depuis le DigiPi :

```bash
cd /home/pi
curl -fsSLO https://raw.githubusercontent.com/Telectroboy/FT8Commander_Improved/main/scripts/collect-v10.7.6.sh
bash ./collect-v10.7.6.sh
```

Le script s'arrête sans produire d'archive si une empreinte ne correspond pas.
L'archive obtenue reste une archive de revue : son contenu doit être inspecté
avant tout import ou publication.
