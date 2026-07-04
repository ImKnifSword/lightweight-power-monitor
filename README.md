# Lightweight Power Monitor

Lekki, minimalistyczny monitor poboru mocy dla starszych maszyn.
Serwer webowy nasłuchuje na `0.0.0.0:5000`, więc możesz zobaczyć dane z dowolnego urządzenia w sieci lokalnej.

## Interfejsy zasilania (fallbacks)
1. `/sys/class/powercap/intel-rapl`  \
2. `/sys/class/power_supply/` (np. AC adapter)  \
3. lm-sensors (`sensors`)  \
4. Estymacja poboru na podstawie obciążenia CPU i zdefiniowanego TDP

Jeśli czujniki są niedostępne, aplikacja szacuje pobór W oraz dzienne/miesięczne koszty.

## Szybki start

```bash
sudo apt-get install -y python3  # na starszych Debian/Ubuntu

# Uruchom aplikację
cd ~/Projects/lightweight-power-monitor
bash start.sh
# lub
python3 power_monitor.py
```

W przeglądarce wejdź na:
- `http://<IP_MASZYNY>:5000`
- `http://localhost:5000`

## Znajdź adres IP

```bash
ip route get 1.1.1.1 | awk '{print $7}'
# lub
hostname -I | awk '{print $1}'
```

## Auto-start po rebootie (systemd)

```bash
sudo cp lightweight-power-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lightweight-power-monitor.service
```

Domyślnie unit zakłada katalog:
`/home/<USERNAME>/Projects/lightweight-power-monitor`

Jeśli używasz innego użytkownika lub ścieżki, popraw `WorkingDirectory` i `ExecStart` w unit file.

## Konfiguracja

Edytuj `CONFIG` w `power_monitor.py`:
- `host` / `port`
- `cpu_tdp_w` i `idle_power_w`
- `sample_interval_s`, `history_max`
- `kwh_price` do kalkulacji kosztów

## Stack
- Backend: Python `http.server`
- Frontend: lekki HTML + Chart.js z CDN
- Brak zależności poza standardową biblioteką
