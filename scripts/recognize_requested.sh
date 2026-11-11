#!/usr/bin/env bash
# Rozpoznawanie zleconych pozycji intake'u bez udziału człowieka (#75).
#
# Uruchamiane z crona na maszynie, na której jest repo i klient Claude Code.
# Sesja jest bezgłowa (`claude -p`) i zamknięta z trzech stron:
#
#   1. TRZY narzędzia — intake_pending, intake_zoom, intake_propose. Nie ma
#      `intake_commit`, więc automat nie ma czym pisać do magazynu; najgorszy
#      skutek jego pomyłki to karta do odrzucenia kliknięciem (ADR-021). Nie
#      ma też `intake_reject`: o powtórzeniu zdjęcia decyduje ten, kto trzyma
#      rzecz w ręce.
#   2. WŁASNA konfiguracja MCP (`scripts/mcp-cron.json`) z
#      `--strict-mcp-config`: serwera Part-DB w niej nie ma, więc magazyn
#      jest niedostępny także do odczytu.
#   3. INTAKE_ONLY_REQUESTED=1 w kontenerze — `intake_pending` oddaje
#      wyłącznie pozycje zlecone przyciskiem w PWA, choćby model poprosił
#      o całą kolejkę. Bez tego „tylko zlecone" byłoby zdaniem w promptcie,
#      a zdanie w promptcie nie jest gwarancją.
#
# Instalacja (crontab użytkownika, który ma zalogowane Claude Code):
#   7 * * * * /path/to/partdb-photo-intake/scripts/recognize_requested.sh
#
# RAZ NA GODZINĘ, nie co dziesięć minut. Decyzja użytkownika: rzadziej budzić
# automat, a w zamian brać większą paczkę (limit=15 w promptcie). Minuta 7,
# nie 0: o pełnej godzinie na API wchodzi cały świat naraz.

set -euo pipefail

# Cron startuje z gołym PATH (`/usr/bin:/bin`) i nie zna `~/.local/bin`, gdzie
# siedzi `claude`. Zmierzone: pierwszy przebieg z crona padł na
# „timeout: failed to run command 'claude': No such file or directory",
# a ten sam skrypt uruchomiony ręcznie działał — bo moja powłoka ma ten
# katalog w PATH. Skrypt musi być samowystarczalny.
export PATH="$HOME/.local/bin:$PATH"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT="$REPO/scripts/recognize_requested.md"
LOG="${BOM_RECOGNIZE_LOG:-$HOME/.local/state/bom-recognize.log}"
LOCK="${BOM_RECOGNIZE_LOCK:-/tmp/bom-recognize.lock}"
# Zmierzone: 8 pozycji w 3 min 13 s, czyli ~24 s na pozycję. Paczka z promptu
# (limit=15) potrzebuje więc ok. 6 minut; 900 s daje zapas na wolniejsze
# zdjęcia i na `intake_zoom` przy nieczytelnych nadrukach. Dłuższe wiszenie
# znaczy, że coś się zawiesiło — i wtedy `timeout` to ubija, zamiast
# zostawiać proces na godziny.
TIMEOUT="${BOM_RECOGNIZE_TIMEOUT:-900}"

# Adres bom-agent po HTTP, bez MCP — do sprawdzenia STANU KOLEJKI wprost
# z bazy, niezależnie od tego, co o sobie powie sesja `claude -p`. Zmierzone:
# trzy przebiegi z rzędu napisały „brak zleceń", mimo że w tym samym czasie
# w kolejce czekało 19 zleconych pozycji — sesja albo dostała błąd
# transportu MCP i po cichu potraktowała go jak pustą odpowiedź, albo
# narzędzie faktycznie oddało puste wyniki z innego powodu; bez niezależnego
# pomiaru nie da się tych dwóch przypadków odróżnić z samego logu.
BOM_URL="${BOM_RECOGNIZE_URL:-http://localhost:8091}"

queue_depth() {
  curl -s -m 5 "$BOM_URL/api/intake?status=pending&limit=200" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("nieznany (odczyt nie powiódł się)")
    raise SystemExit
items = data.get("items", [])
flagged = [i for i in items if i.get("recognize_requested_at")]
awaiting = [i for i in flagged if i.get("proposals_pending")]
print(f"{len(flagged)} zleconych, {len(flagged) - len(awaiting)} do wzięcia, "
      f"{len(awaiting)} już z propozycją")
' 2>/dev/null || echo "nieznany (curl nie powiódł się)"
}

# Ile pozycji jest DO WZIĘCIA. Puste = nie ma po co budzić sesji.
# `-1` znaczy „nie wiem" (usługa nie odpowiedziała) — wtedy wolimy odpalić
# sesję i pozwolić jej samej stwierdzić stan, niż przespać realną robotę
# z powodu jednego nieudanego `curl`.
pending_work() {
  curl -s -m 5 "$BOM_URL/api/intake?status=pending&limit=200" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(-1)
    raise SystemExit
items = data.get("items", [])
flagged = [i for i in items if i.get("recognize_requested_at")]
print(len([i for i in flagged if not i.get("proposals_pending")]))
' 2>/dev/null || echo -1
}

mkdir -p "$(dirname "$LOG")"

# Jeden przebieg naraz. Bez tego wolniejszy przebieg spotkałby się
# z następnym i oba rozpoznawałyby tę samą pozycję.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) pomijam — poprzedni przebieg jeszcze trwa" >>"$LOG"
  exit 0
fi

# Nie budzimy sesji, gdy nie ma czego rozpoznać. Zmierzone: przez siedem
# godzin cron odpalał `claude -p` co dziesięć minut tylko po to, żeby
# napisać „brak zleceń" — czterdzieści kilka sesji, z których każda zostawia
# po sobie plik rozmowy i zżera limit subskrypcji, nie wnosząc niczego.
# Stan kolejki to jedno tanie zapytanie REST; sesja to kilkadziesiąt sekund
# pracy modelu. Sprawdzamy to, co tanie, przed tym, co drogie.
WORK="$(pending_work)"
if [ "$WORK" = "0" ]; then
  echo "$(date -Is) nic do rozpoznania — nie odpalam sesji" >>"$LOG"
  exit 0
fi

{
  echo "── $(date -Is) start · kolejka: $(queue_depth)"
  cd "$REPO"
  # `</dev/null`: bez tego klient czeka trzy sekundy na dane ze standardowego
  # wejścia, których cron nigdy nie przyśle, i zapisuje o tym ostrzeżenie.
  timeout "$TIMEOUT" claude -p "$(cat "$PROMPT")" \
    --mcp-config "$REPO/scripts/mcp-cron.json" \
    --strict-mcp-config \
    --allowed-tools "mcp__bom__intake_pending,mcp__bom__intake_zoom,mcp__bom__intake_propose" \
    --output-format text </dev/null 2>&1 || echo "!! przebieg zakończony kodem $?"
  echo "── $(date -Is) koniec · kolejka: $(queue_depth)"
} >>"$LOG" 2>&1

# Log ma służyć diagnozie ostatnich przebiegów, nie archiwizacji. Bez
# przycięcia rośnie bez końca na maszynie, na której nikt go nie czyta.
if [ "$(wc -l <"$LOG")" -gt 2000 ]; then
  tail -n 1000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
