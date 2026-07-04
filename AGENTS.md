# lightweight-power-monitor

## Created
2026-07-04 via Discord

## Tech Stack
- Language: Python 3.11.2
- Framework: Built-in http.server + vanilla HTML/JS + Chart.js
- Testing: brak formalnych testów
- Linting: brak

## Key Structure
- `power_monitor.py` - główny plik: backend + frontend w HTML_TEMPLATE
- `lightweight-power-monitor.service` - systemd unit
- `start.sh` - skrypt startowy

## Architectural Decisions
- Brak zewnętrznych zależności poza standardową biblioteką Pythona
- Dane pobierane z: Intel RAPL, power_supply, lm-sensors lub estymacja CPU
- Historia przechowywana w pamięci, ograniczona do 1800 próbek

## Session Log
- 2026-07-04: Dodanie dashboardu procesów przez `/api/processes`, osobnego template i hooków JS
