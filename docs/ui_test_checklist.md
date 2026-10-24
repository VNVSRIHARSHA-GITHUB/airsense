# AirSense — Manual UI Test Checklist

Manual checks of the two web pages, run in a browser against the real system. The API and the logic behind the pages are covered by the automated tests (`pytest`); these checks cover what only a person can judge: layout, wording, and behaviour when something fails. The results feed the test report (Stage 11).

**Setup:** model server (`bash model_server/serve.sh`), backend (`uvicorn main:app --app-dir backend --port 8000`) and frontend (`python -m http.server 8080 --directory frontend`) running; live features refreshed (`dvc repro feast_online`) less than 4 hours ago, unless a check says otherwise. Browser: Chrome or Firefox; phone checks with the browser's device toolbar (F12, then Ctrl+Shift+M) at 400 px width, or a real phone.

**How to record:** write Pass or Fail, the date, and a note for every Fail (what happened, screenshot name).

## Forecast page

| ID | Check | Steps | Expected | Result |
|---|---|---|---|---|
| F1 | Page loads | Open http://localhost:8080 | Title, 14 city tiles with values and categories, "Latest measurement … ago", one city's details shown | |
| F2 | City details | Click Delhi | Coloured box with current value, category, time, stations; outlook sentence; 24 coloured hour cells; line chart; advice; "Why this forecast" with 4 factors | |
| F3 | Switching cities | Click three other cities | Details change to each city; the selected tile is outlined | |
| F4 | Advice groups | Switch "Advice for" through the 3 choices | Advice text and its heading change; forecast numbers stay the same | |
| F5 | AQI colours and labels | Compare tiles with the table in "How to read this page" | Each tile's colour matches its category; the category is also written as text | |
| F6 | Hover values | Hover over an hour cell and a chart point | Tooltip with time, µg/m³ and category | |
| F7 | Notes | Open a city with missing history (if any) | Yellow "Notes on this forecast" box naming the missing input | |
| F8 | No forecast (stale data) | Do not refresh live features for more than 4 h, then open a city | "No forecast for … right now", reason with the age, last known reading, other cities in the region | |
| F9 | Model server down | Stop the model server, click a city | "Could not load the forecast … Model server unavailable"; page still usable | |
| F10 | Backend down | Stop the backend, press Refresh | Red bar explaining the service cannot be reached | |
| F11 | Loading message | Restart the backend, click a city immediately | "Loading the forecast for …" with a spinner until the details appear | |
| F12 | Refresh | Press Refresh after `dvc repro feast_online` | "Latest measurement … ago" updates | |
| F13 | Reload keeps controls consistent | Choose "Outdoor workers", reload (F5) | Controls and advice heading match after the reload | |
| F14 | Help section | Open "How to read this page" | Explanation of PM2.5, the AQI table with colours, explanation of the forecast, factors and notes | |
| F15 | Keyboard use | Use Tab and Enter only | Every tile and control can be reached; a visible focus outline shows where you are | |

## Forecast page on a phone (400 px wide)

| ID | Check | Steps | Expected | Result |
|---|---|---|---|---|
| P1 | Phone layout | Open the page at phone width | City and "Advice for" dropdowns at the top; no tiles; no horizontal scrolling of the page | |
| P2 | City dropdown | Open the City menu | Cities grouped by region, names with the state in brackets, no values | |
| P3 | Details | Choose a city | Details directly below the controls, readable without zooming | |
| P4 | Across India list | Scroll to the bottom, tap a city in the list | Ranked list of 14 cities; tapping opens that city and scrolls to its details | |

## Pipeline console

| ID | Check | Steps | Expected | Result |
|---|---|---|---|---|
| C1 | Page loads | Open http://localhost:8080/pipeline.html | Four summary blocks, the pipeline graph, collection timeline, live model, speed chart, steps table, warnings | |
| C2 | Step status matches DVC | Run `dvc status`, compare with the graph and table | Same steps shown as changed / up to date (the console may be up to 1 min behind) | |
| C3 | Step details | Click a step in the graph and a name in the table | Details show the command, last run, report values, and the reason if it changed | |
| C4 | Collection timeline | Compare pink cells with the snapshot files in `data/raw/live/datagov/` | Missing half-hours are pink; gaps listed with times | |
| C5 | Model and gate | Compare with `data/interim/registry.json` and `gate_report.json` | Same version, metrics and 5 check results | |
| C6 | Speed chart | Compare with the step reports in `data/interim/` | One bar per step that reports timing; values match | |
| C7 | Warnings | Compare with the end of `logs/collector.log` | Latest warnings and errors listed, newest first; no API key visible | |
| C8 | Backend down | Stop the backend, press Refresh | Red bar explaining the status cannot be loaded | |
| C9 | Phone width | Open at 400 px | Blocks stacked; graph can be scrolled sideways; table scrolls sideways; long log lines wrap | |

## Result summary

| Date | Tester | Checks run | Passed | Failed (IDs) |
|---|---|---|---|---|
| | | | | |
