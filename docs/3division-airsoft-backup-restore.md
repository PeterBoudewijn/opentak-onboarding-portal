# 3Division airsoft backup en restore

Deze procedure is bedoeld voor de productieomgeving met:

- portal: `/opt/opentak-onboarding-portal`
- OpenTAKServer data: `/home/team3d/ots-1713`
- OTS Python: `/home/team3d/.opentakserver_venv_1713/bin/python`

De backup bevat gevoelige gegevens: portal-secrets, databases, TAK CA-materiaal en certificaten. Bewaar het archief daarom alleen root-leesbaar en kopieer het daarna naar versleutelde/off-host opslag.

## Backup maken

```bash
cd /opt/opentak-onboarding-portal

sudo bash   scripts/installers/backup_3division_airsoft_stack.sh
```

De backup wordt standaard geschreven naar:

```text
/var/backups/3division-airsoft/
```

De backupscript maakt onder andere:

- een volledige Git bundle van de portal;
- de productie-`.env`;
- portal runtime-data zoals `instance/`, `datapackages/` en `updates/`;
- een consistente portal-databasebackup;
- een consistente OTS-databasebackup;
- het volledige `OTS_DATA_FOLDER`, inclusief CA/certificaatdata;
- relevante systemd/Nginx-configuratie;
- Python/npm-versie-informatie;
- SHA-256 checksums van alle onderdelen.

Na het maken wordt de backup automatisch non-destructief geverifieerd.

## Bestaande backup opnieuw controleren

```bash
sudo bash   /opt/opentak-onboarding-portal/scripts/installers/verify_3division_airsoft_backup.sh   /var/backups/3division-airsoft/3division-airsoft-YYYYMMDD-HHMMSS.tar.gz
```

Een bruikbare backup eindigt met:

```text
FAIL=0
RESULT: PASS
```

## Non-destructieve restore-rehearsal

Na een geslaagde backupverificatie kan de volledige herstelketen worden getest zonder productie te wijzigen:

```bash
sudo bash \
  /opt/opentak-onboarding-portal/scripts/installers/verify_3division_airsoft_restore_rehearsal.sh \
  /var/backups/3division-airsoft/3division-airsoft-YYYYMMDD-HHMMSS.tar.gz
```

Deze rehearsal:

- cloneert de portal vanuit de Git bundle naar een tijdelijke map;
- pakt portal runtime-data uit;
- opent en controleert een kopie van de portal SQLite-database;
- herstelt de OTS PostgreSQL-dump in een tijdelijke testdatabase;
- pakt OTS-data/CA en systeemconfig uit;
- vergelijkt de herstelde root truststore met productie wanneer die aanwezig is;
- verwijdert de tijdelijke PostgreSQL-database en bestanden automatisch.

Een succesvolle rehearsal eindigt met:

```text
RESTORE REHEARSAL: PASS
Productie is NIET gewijzigd.
```

## Restore-principe

Een productie-restore is bewust niet volledig geautomatiseerd. Een restore wist of vervangt databases, portal-state en OTS CA-data. Daarom moet een operator eerst de backup verifiëren en daarna expliciet de onderdelen terugzetten.

De veilige volgorde is:

1. verifier van de gekozen backup uitvoeren;
2. portal en OTS stoppen;
3. backup uitpakken in een tijdelijke map;
4. portal-code en `.env` herstellen;
5. portal runtime-data herstellen;
6. portal-database herstellen;
7. OTS-database herstellen;
8. `OTS_DATA_FOLDER` inclusief CA/certificaten herstellen;
9. relevante systemd/Nginx-config terugzetten indien nodig;
10. dependencies/venvs herstellen indien het om een nieuwe host gaat;
11. de normale self-healing deploy uitvoeren;
12. de complete stack-verifier laten eindigen op `FAIL=0`.

## Backup uitpakken voor inspectie

Dit raakt productie niet:

```bash
sudo mkdir -p /var/tmp/3division-airsoft-restore-test
sudo tar -xzf   /var/backups/3division-airsoft/3division-airsoft-YYYYMMDD-HHMMSS.tar.gz   -C /var/tmp/3division-airsoft-restore-test

sudo cat /var/tmp/3division-airsoft-restore-test/manifest.txt
```

De manifest bevat geen wachtwoorden; alleen backupmetadata zoals commit, database-backend, databasenaam en databaserol.

## Productie stoppen voor een echte restore

Alleen uitvoeren wanneer echt hersteld moet worden:

```bash
sudo systemctl stop opentak-portal
sudo systemctl stop opentakserver-1713.service
```

Nginx en RabbitMQ hoeven voor het terugzetten van portal/OTS-bestanden niet per se gestopt te worden.

## Portal-code herstellen

Bij een volledige host-recovery kan de Git bundle als bron worden gebruikt:

```bash
sudo mv   /opt/opentak-onboarding-portal   /opt/opentak-onboarding-portal.pre-restore

sudo git clone   /var/tmp/3division-airsoft-restore-test/portal/portal.bundle   /opt/opentak-onboarding-portal

PORTAL_COMMIT="$(
  sudo awk -F= '$1=="portal_commit"{print $2}'   /var/tmp/3division-airsoft-restore-test/manifest.txt
)"

sudo git -C /opt/opentak-onboarding-portal   checkout --detach "${PORTAL_COMMIT}"

sudo chown -R opentak-portal:opentak-portal   /opt/opentak-onboarding-portal
```

Daarna de productie-`.env` terugzetten:

```bash
sudo cp   /var/tmp/3division-airsoft-restore-test/portal/.env   /opt/opentak-onboarding-portal/.env

sudo chown opentak-portal:opentak-portal   /opt/opentak-onboarding-portal/.env

sudo chmod 600   /opt/opentak-onboarding-portal/.env
```

Portal runtime-data:

```bash
sudo tar -xzf   /var/tmp/3division-airsoft-restore-test/portal/runtime-data.tar.gz   -C /opt/opentak-onboarding-portal
```

## OTS-data / CA herstellen

De OTS-data bevat de TAK CA en moet als één samenhangend geheel worden behandeld.

Maak bij een same-host restore eerst een extra veiligheidskopie:

```bash
sudo mv   /home/team3d/ots-1713   /home/team3d/ots-1713.pre-restore
```

Herstel daarna:

```bash
sudo tar --same-owner -xzf   /var/tmp/3division-airsoft-restore-test/ots/ots-data.tar.gz   -C /home/team3d
```

Controleer daarna minimaal:

```bash
sudo test -f /home/team3d/ots-1713/ca/truststore-root.p12 &&
echo "PASS: OTS root truststore aanwezig"
```

## PostgreSQL database herstellen

Lees eerst de databasenaam en rol uit de manifest:

```bash
RESTORE_DIR=/var/tmp/3division-airsoft-restore-test

PORTAL_DB="$(
  sudo awk -F= '$1=="portal_db_name"{print $2}'   "${RESTORE_DIR}/manifest.txt"
)"
PORTAL_ROLE="$(
  sudo awk -F= '$1=="portal_db_role"{print $2}'   "${RESTORE_DIR}/manifest.txt"
)"
OTS_DB="$(
  sudo awk -F= '$1=="ots_db_name"{print $2}'   "${RESTORE_DIR}/manifest.txt"
)"
OTS_ROLE="$(
  sudo awk -F= '$1=="ots_db_role"{print $2}'   "${RESTORE_DIR}/manifest.txt"
)"
```

Voor een PostgreSQL-dump moet eerst worden gecontroleerd of de rol bestaat:

```bash
sudo -u postgres psql -tAc   "SELECT 1 FROM pg_roles WHERE rolname='${OTS_ROLE}'"
```

Een database-restore is destructief. Stop eerst de services en gebruik pas daarna `pg_restore`. Op een verse database kan bijvoorbeeld:

```bash
sudo -u postgres dropdb --if-exists "${OTS_DB}"
sudo -u postgres createdb -O "${OTS_ROLE}" "${OTS_DB}"

sudo -u postgres pg_restore   --no-owner   --role="${OTS_ROLE}"   --dbname="${OTS_DB}"   "${RESTORE_DIR}/database/ots.dump"
```

Gebruik dezelfde methode voor de portal-database als `portal_db_backend=postgresql`.

Wanneer de portal SQLite gebruikt, wordt `database/portal.sqlite` rechtstreeks naar het in de manifest beschreven databasepad teruggezet. Gebruik dan geen `pg_restore`.

Als portal en OTS onverhoopt dezelfde PostgreSQL-databasenaam gebruiken, herstel die database maar één keer en onderzoek eerst welke dump als autoritatief moet gelden.

## Systemd/Nginx terugzetten

Alleen nodig bij een nieuwe host of beschadigde configuratie:

```bash
sudo tar --same-owner -xzf   /var/tmp/3division-airsoft-restore-test/system/system-config.tar.gz   -C /

sudo systemctl daemon-reload
sudo nginx -t
```

Web-TLS-certificaten van Let's Encrypt zitten bewust niet in deze backup; die kunnen opnieuw worden uitgegeven. De TAK CA/certificaatstate zit wel in `OTS_DATA_FOLDER`.

## Afronden na restore

Voer altijd de normale deploy uit. Die bouwt de frontend opnieuw en brengt alle vereiste OTS-patches weer aan:

```bash
cd /opt/opentak-onboarding-portal

sudo bash   scripts/installers/deploy_3division_airsoft_update.sh
```

Omdat de deploy zichzelf verifieert, moet hij eindigen met:

```text
FAIL=0
RESULT: PASS
Post-deploy verifier passed.
```

Start tenslotte de services indien dat nog nodig is:

```bash
sudo systemctl restart rabbitmq-server
sudo systemctl restart opentakserver-1713.service
sudo systemctl restart opentak-portal
sudo systemctl reload nginx
```

## Automatische dagelijkse backups

Installeer de systemd-timer:

```bash
sudo bash \
  /opt/opentak-onboarding-portal/scripts/installers/install_3division_airsoft_backup_timer.sh
```

Standaard draait deze iedere dag om 02:30 Europe/Amsterdam. Iedere geplande backup wordt volledig geverifieerd voordat retentie wordt toegepast.

Geplande backups krijgen een eigen naam:

```text
3division-airsoft-scheduled-YYYYMMDD-HHMMSS.tar.gz
```

Standaard blijven de 7 nieuwste geplande backups bewaard. Handmatige/golden backups zonder `scheduled` in de naam worden nooit door deze retentie verwijderd.

Controleer de timer met:

```bash
systemctl list-timers 3division-airsoft-backup.timer
systemctl status 3division-airsoft-backup.timer
```

Een geplande backup handmatig testen:

```bash
sudo systemctl start 3division-airsoft-backup.service
sudo journalctl -u 3division-airsoft-backup.service -n 100 --no-pager
```

De retentie kan voor een handmatige run worden aangepast:

```bash
sudo AIRSOFT_BACKUP_RETENTION_COUNT=14 bash \
  /opt/opentak-onboarding-portal/scripts/installers/run_3division_airsoft_scheduled_backup.sh
```

## Aanbevolen bewaarbeleid

- minimaal 1 recente backup off-host;
- daarnaast 3 tot 7 historische backups;
- altijd een nieuwe backup vóór OTS-, database- of CA-wijzigingen;
- periodiek de oudste bewaarde backup met de backup-verifier testen;
- het backupbestand zelf versleuteld opslaan buiten de TAK-server.
