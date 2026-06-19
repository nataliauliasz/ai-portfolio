# Produkcyjne wdrozenie snapshotu rankingu

Ten dokument opisuje kompletne wdrozenie dziennego snapshotu rankingu dla Linux + cron. Zakladka `Ranking` pozostaje widokiem read-only: web odczytuje tylko ostatni gotowy snapshot z `public.charging_log_ranking_snapshot`.

Jesli srodowisko dziala na Windows, uzyj osobnej instrukcji [ranking_snapshot_windows_task.md](./ranking_snapshot_windows_task.md).

## 1. Wymagania

- Python 3.12+
- Dostep do PostgreSQL z uprawnieniami do:
  - `SELECT` na zrodle danych
  - wywolania `public.refresh_charging_log_processed_mv()`
  - wywolania `public.refresh_charging_log_sessions_mv()`
  - `SELECT`, `INSERT`, `UPDATE` na `public.charging_log_ranking_snapshot`
- Linux z dostepnym `cron` i `flock`

## 2. Bootstrap bazy

Wykonaj SQL w tej kolejnosci:

1. `sql/001_create_charging_log_processed_mv.sql`
2. `sql/003_create_charging_log_sessions_mv.sql`
   alternatywnie `sql/004_create_charging_log_sessions_table.sql`, jesli srodowisko nie pozwala na `CREATE MATERIALIZED VIEW`
3. `sql/002_create_charging_log_ranking_snapshot.sql`

Po wdrozeniu sprawdz:

- istnieje `public.charging_log_processed_mv`
- istnieje `public.charging_log_sessions_mv`
- istnieje `public.charging_log_ranking_snapshot`
- konto produkcyjne moze uruchomic oba `refresh_*`

## 3. Konfiguracja aplikacji

Ustaw zmienne srodowiskowe w `.env`, `.env.local`, `db.env` albo `db.local.env`:

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSLMODE` opcjonalnie, domyslnie `require`
- `DB_SOURCE_RELATION` zostaw jako domyslne `public.charging_log_processed_mv`, jesli produkcja ma korzystac z precomputed session source

Wrapper `run_ranking_snapshot.sh`:

- laduje powyzsze pliki env
- nie nadpisuje juz ustawionych zmiennych procesu
- tworzy logi w `logs/`
- uzywa `flock`, aby drugi job nie uruchomil sie rownolegle
- zawsze odpala `python generate_ranking_snapshot.py --refresh-source-view`

## 4. Test manualny przed cronem

Uruchom z katalogu aplikacji:

```bash
chmod +x run_ranking_snapshot.sh
./run_ranking_snapshot.sh
```

Zweryfikuj:

- skrypt konczy sie kodem `0`
- powstaje plik `logs/ranking_snapshot_*.log`
- w bazie pojawia sie lub aktualizuje rekord dla biezacej `snapshot_date`

Przykladowa kontrola w SQL:

```sql
select snapshot_date, generated_at, source_relation, source_row_count, session_count
from public.charging_log_ranking_snapshot
order by snapshot_date desc, generated_at desc
limit 5;
```

## 5. Instalacja crona

Przyklad dla `/opt/charging-log-app`:

```cron
CRON_TZ=Europe/Warsaw
0 3 * * * cd /opt/charging-log-app && /usr/bin/env bash ./run_ranking_snapshot.sh >> /opt/charging-log-app/logs/cron.log 2>&1
```

Gotowy przyklad jest tez w `ops/ranking_snapshot.cron.example`.

Uwagi:

- `CRON_TZ=Europe/Warsaw` zapewnia start o 03:00 czasu warszawskiego nawet wtedy, gdy system ma inna strefe.
- jesli serwer juz pracuje w `Europe/Warsaw`, wpis moze zostac bez `CRON_TZ`, ale jawne ustawienie jest bezpieczniejsze
- cron ma uruchamiac ten sam wrapper, ktory byl sprawdzony recznie

## 6. Weryfikacja po wdrozeniu

Po nocnym jobie sprawdz:

- `logs/cron.log`
- najnowszy `logs/ranking_snapshot_*.log`
- rekord w `public.charging_log_ranking_snapshot`
- zakladke `Ranking` w UI

Oczekiwane zachowanie:

- `Ranking` pokazuje najnowszy snapshot bez dodatkowego przeliczania w request-cie
- jesli nocny job nie wykonal sie, UI nadal pokazuje ostatni zapisany snapshot z komunikatem o starej dacie
- jesli tabela snapshotow jest pusta, UI pokazuje komunikat o braku snapshotu
