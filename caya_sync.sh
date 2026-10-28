#!/bin/bash
# Wöchentlicher Lauf: Caya-Dokumente holen, neue in Dokumente ablegen,
# dem Heimmonitor melden. Wird von launchd aufgerufen (de.lenny.caya-sync).

set -u
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJEKT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJEKT" || exit 1
PY="$PROJEKT/.venv/bin/python"

mkdir -p downloads/logs
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
LOG="downloads/logs/${STAMP}.log"
FOLGE_DATEI="downloads/fehler_in_folge.txt"

{
  echo "Lauf: $(date '+%Y-%m-%d %H:%M:%S')"

  # Erst auf Netz warten. launchd holt einen verschlafenen Termin beim
  # Aufwachen sofort nach - dann steht das WLAN oft noch nicht, und der Lauf
  # scheiterte schon an der Namensaufloesung (18. und 25.10.2026, beide
  # unbemerkt). Bis zu zehn Minuten Geduld.
  for _ in $(seq 1 40); do
    curl -s -m 5 -o /dev/null https://cognito-idp.eu-central-1.amazonaws.com/ && break
    sleep 15
  done || true
  curl -s -m 5 -o /dev/null https://cognito-idp.eu-central-1.amazonaws.com/ \
    || echo "Kein Netz nach 10 Minuten - versuche es trotzdem"

  echo "── Abruf ──"
  "$PY" -u caya_fetch.py
  RC=$?
  if [ "$RC" -eq 3 ]; then
    # Sitzung abgelaufen: mit den Zugangsdaten aus .env neu anmelden, nochmal
    echo "── Sitzung abgelaufen, neue Anmeldung ──"
    rm -f .auth/tokens.json
    if "$PY" -u caya_login.py; then
      "$PY" -u caya_fetch.py
      RC=$?
    fi
  fi

  if [ "$RC" -eq 0 ] || [ "$RC" -eq 1 ]; then
    # Bei einzelnen Download-Fehlern (1) trotzdem ablegen, was da ist
    echo "── Import ──"
    "$PY" -u caya_import.py --apply
    RC_IMPORT=$?
    [ "$RC_IMPORT" -ne 0 ] && RC=$RC_IMPORT
  fi
  echo "── Ende (Exit-Code $RC) ──"
  echo "RC=$RC"
} > "$LOG" 2>&1

RC="$(grep -oE '^RC=[0-9]+' "$LOG" | tail -1 | cut -d= -f2)"
RC="${RC:-1}"

# Fehler in Folge zählen
if [ "$RC" -eq 0 ]; then
  echo 0 > "$FOLGE_DATEI"
else
  echo $(( $(cat "$FOLGE_DATEI" 2>/dev/null | tr -cd '0-9' || echo 0) + 1 )) > "$FOLGE_DATEI"
fi
FOLGE="$(cat "$FOLGE_DATEI" | tr -cd '0-9')"

# Kurzes Ergebnis für den Heimmonitor
zahl() { grep -oE "$1" "$LOG" | tail -1 | grep -oE '[0-9]+' | head -1; }
N_NEU="$(zahl 'Fertig: [0-9]+ neu')"
N_DA="$(zahl '[0-9]+ schon da')"
ABGELEGT="$(grep -oE '^Plan: .*' "$LOG" | tail -1 | grep -oE '[0-9]+ neu' | grep -oE '[0-9]+')"
VERSCHOBEN="$(grep -oE '^Plan: .*' "$LOG" | tail -1 | grep -oE '[0-9]+ verschieben' | grep -oE '[0-9]+')"
UEBERSPRUNGEN="$(grep -oE '^Plan: .*' "$LOG" | tail -1 | grep -oE '[0-9]+ übersprungen' | grep -oE '[0-9]+')"
FEHLER="$(grep -E 'FEHLER|fehlgeschlagen|Traceback|Error' "$LOG" | grep -v ' 0 Fehler' | head -1 | cut -c1-120)"
if [ "$RC" -eq 0 ]; then
  ERGEBNIS="$(( ${N_NEU:-0} + ${N_DA:-0} )) Dokumente in Caya; neu abgelegt: ${ABGELEGT:-0}, verschoben: ${VERSCHOBEN:-0}"
  [ "${UEBERSPRUNGEN:-0}" -gt 0 ] && ERGEBNIS="$ERGEBNIS, nicht verschiebbar: $UEBERSPRUNGEN"
else
  ERGEBNIS="Exit $RC: ${FEHLER:-siehe Log $STAMP}"
fi
ERG_SAUBER="$(printf '%s' "$ERGEBNIS" | tr -d '"\\' | head -c 180)"
echo "$ERG_SAUBER"

# Dem Heimmonitor melden - darf den Lauf nie stoeren
HEIMMONITOR="$(grep -E '^HEIMMONITOR_URL=' .env 2>/dev/null | cut -d= -f2-)"
if [ -n "$HEIMMONITOR" ]; then
  if [ "$RC" -eq 0 ]; then OK=true; else OK=false; fi
  # Mehrere Versuche: faellt der Lauf wegen fehlenden Netzes aus, fehlt das
  # Netz oft auch fuer die Meldung - und ein Ausfall, den keiner meldet,
  # bleibt unbemerkt.
  GEMELDET=nein
  for _ in 1 2 3 4 5; do
    if curl -sf -m 10 -X POST -H "Content-Type: application/json" \
      -d "{\"quelle\":\"caya\",\"name\":\"Caya-Post\",\"ok\":${OK},\"exit\":${RC},\"fehler_in_folge\":${FOLGE:-0},\"takt_stunden\":168,\"ergebnis\":\"${ERG_SAUBER}\"}" \
      "$HEIMMONITOR"; then GEMELDET=ja; break; fi
    sleep 30
  done
  [ "$GEMELDET" = ja ] || echo "Heimmonitor nicht erreichbar (nicht kritisch)"
  echo
fi

exit "$RC"
