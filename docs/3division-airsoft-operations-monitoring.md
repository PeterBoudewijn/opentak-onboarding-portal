# 3Division operationele monitoring

Deze monitor draait iedere 15 minuten als systemd-timer en controleert:

- portal, OpenTAKServer, RabbitMQ, Nginx, Prosody, Mumble en fail2ban;
- schijfruimte;
- de dagelijkse backup-timer;
- leeftijd van de nieuwste geplande backup (maximaal 30 uur);
- resultaat van de laatste backup-run;
- publieke HTTPS-bereikbaarheid;
- vervaldatum van Let's Encrypt-certificaten;
- certbot-timer;
- systeemtijdsynchronisatie;
- onverwacht gefaalde systemd-units.

Standaard waarschuwing voor schijfruimte is 80%, critical is 90%.
Certificaten geven een warning onder 30 dagen en een failure onder 7 dagen.

## Installeren

```bash
cd /opt/opentak-onboarding-portal

sudo bash \
  scripts/installers/install_3division_airsoft_monitor_timer.sh
```

De installer voert direct een eerste controle uit.

## Status bekijken

```bash
sudo cat /var/lib/3division-airsoft-monitor/last-status.txt
```

```bash
systemctl status 3division-airsoft-monitor.timer
systemctl list-timers 3division-airsoft-monitor.timer
```

```bash
sudo journalctl -u 3division-airsoft-monitor.service -n 100 --no-pager
```

## Handmatig uitvoeren

```bash
sudo bash \
  /opt/opentak-onboarding-portal/scripts/installers/check_3division_airsoft_operations.sh
```

De monitor is bewust lichtgewicht. Hij vervangt de uitgebreide
`verify_3division_airsoft_stack.sh` niet; die hoort na deployments en grotere
wijzigingen te worden uitgevoerd.
