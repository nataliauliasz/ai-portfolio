# Ranking snapshot on Windows Task Scheduler

Ten dokument opisuje zalecana konfiguracje dziennego snapshotu rankingu na Windows. Jest potrzebny wtedy, gdy aplikacja nie dziala na Linux `cron`, tylko na laptopie albo serwerze z Harmonogramem zadan.

## 1. Problem, ktory ten wariant naprawia

Task ustawiony jako:

- `LogonType = Interactive`
- `StartWhenAvailable = False`
- `DisallowStartIfOnBatteries = True`
- `WakeToRun = False`

bedzie regularnie pomijal nocny snapshot, gdy:

- uzytkownik nie bedzie zalogowany o `03:00`
- komputer bedzie spal i obudzi sie dopiero rano
- laptop bedzie pracowal na baterii

To dokladnie prowadzi do sytuacji, w ktorej UI pokaze komunikat `Pokazano ostatni zapisany snapshot z ...`.

## 2. Co jest przygotowane w repo

- [ops/register_ranking_snapshot_task.ps1](../ops/register_ranking_snapshot_task.ps1)
  - rejestruje task z `StartWhenAvailable = True`
  - pozwala uruchamiac task poza aktywna sesja interaktywna
  - dopuszcza start na baterii
  - opcjonalnie ustawia `WakeToRun = True`
- [ops/run_ranking_snapshot_dynamic.ps1](../ops/run_ranking_snapshot_dynamic.ps1)
  - zapisuje bardziej kompletne logi
  - loguje wybrany interpreter Pythona
  - zwraca czytelny blad z kodem wyjscia
- [run_ranking_snapshot.ps1](../run_ranking_snapshot.ps1)
  - wariant docelowy bez fallbacku dynamicznego

## 3. Ktory tryb wybrac

Sa dwa tryby rejestracji taska:

- `dynamic`
  - uzywa `ops/run_ranking_snapshot_dynamic.ps1`
  - uruchamia `generate_ranking_snapshot.py --refresh-source-view --allow-dynamic-session-fallback`
  - wybierz ten tryb, jesli w bazie nadal nie istnieje `public.charging_log_sessions_mv`
- `precomputed`
  - uzywa [run_ranking_snapshot.ps1](../run_ranking_snapshot.ps1)
  - uruchamia `generate_ranking_snapshot.py --refresh-source-view`
  - wybierz ten tryb po wdrozeniu `sql/003_create_charging_log_sessions_mv.sql` albo `sql/004_create_charging_log_sessions_table.sql`

W aktualnym stanie tej instancji bezpieczniejszy jest tryb `dynamic`, bo `public.charging_log_sessions_mv` nie istnieje.

## 4. Rejestracja taska

Uruchom PowerShell jako uzytkownik, pod ktorym task ma dzialac, i wykonaj:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\register_ranking_snapshot_task.ps1 -Mode dynamic
```

Skrypt poprosi o haslo dla konta `DOMAIN\user` i zarejestruje task:

- codziennie o `03:00`
- z `StartWhenAvailable = True`
- z `AllowStartIfOnBatteries = True`
- z `DontStopIfGoingOnBatteries = True`
- z `WakeToRun = True`

Jesli po wdrozeniu relacji sesyjnej chcesz przejsc na wariant docelowy:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\register_ranking_snapshot_task.ps1 -Mode precomputed
```

## 5. Weryfikacja po rejestracji

Sprawdz task:

```powershell
Get-ScheduledTask -TaskName 'Charging Log Ranking Snapshot' | Format-List TaskName,State
Get-ScheduledTaskInfo -TaskName 'Charging Log Ranking Snapshot' | Format-List LastRunTime,LastTaskResult,NextRunTime,NumberOfMissedRuns
```

Wymus reczne uruchomienie:

```powershell
Start-ScheduledTask -TaskName 'Charging Log Ranking Snapshot'
```

Nastepnie zweryfikuj:

- najnowszy plik `logs/ranking_snapshot_*.log`
- rekord w `public.charging_log_ranking_snapshot`
- czy `snapshot_date` i `generated_at` przesunely sie do biezacej daty

Przykladowe zapytanie kontrolne:

```sql
select snapshot_date, generated_at, source_relation, source_row_count, session_count
from public.charging_log_ranking_snapshot
order by snapshot_date desc, generated_at desc
limit 5;
```

## 6. Dodatkowa uwaga operacyjna

Samo naprawienie taska nie rozwiazuje wszystkiego, jesli baza nie ma precomputed session source. Docelowo warto wdrozyc:

1. `sql/003_create_charging_log_sessions_mv.sql`
2. albo `sql/004_create_charging_log_sessions_table.sql`
3. potem przelaczyc task na `-Mode precomputed`
