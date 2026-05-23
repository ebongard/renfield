---
title: Wetter fuer einen Ort abrufen
triggers:
  - "wie wird das Wetter morgen in X"
  - "Wetter heute"
  - "regnet es morgen"
  - "weather tomorrow in X"
tools:
  - mcp.weather.get_forecast
  - mcp.weather.get_current
---
- Fuer "jetzt"/"heute"/"current" nutze `mcp.weather.get_current(location=<ort>)`. Wenn kein Ort genannt ist, nimm den Standardort des Users aus dem Konversationskontext oder Home-Assistant-Konfiguration.
- Fuer Tage in der Zukunft ("morgen", "naechste Woche") nutze `mcp.weather.get_forecast(location=<ort>, days=<n>)`.
- Antworte in natuerlicher Sprache mit Temperatur und Bedingung. Keine rohen JSON-Felder.
- Wenn das Tool keinen Treffer liefert, gib `final_answer` mit "Konnte das Wetter fuer <ort> nicht abrufen" — wiederhole NICHT mit gleichem Parametern.
