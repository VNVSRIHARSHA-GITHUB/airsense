# AirSense — Manual UI Test Checklist

Manual checks of the two web pages, run in a browser against the real system. The API and the logic behind the pages are covered by the automated tests (`pytest`); these checks cover what only a person can judge: layout, wording, and behaviour when something fails. The results feed the test report (Stage 11).

**Setup:** the containers running (`docker compose up -d`, see `docs/architecture.md` §8), or on the laptop the model server (`bash model_server/serve.sh`), backend (`uvicorn main:app --app-dir backend --port 8000`) and frontend (`python -m http.server 8080 --directory frontend`) with Redis and Qdrant (`docker compose up -d redis qdrant`); in the container setup, "stop the model server" means `docker compose stop model-server` (and likewise for the backend); live features refreshed (`dvc repro feast_online`) less than 4 hours ago, unless a check says otherwise. Browser: Chrome or Firefox; phone checks with the browser's device toolbar (F12, then Ctrl+Shift+M) at 400 px width, or a real phone.

The checks below are for the current layout (tabs, light and dark theme, hover details; redesigned on 1 Nov 2026 from a single long page per view).

**How to record:** write Pass or Fail, the date, and a note for every Fail (what happened, screenshot name).

## Forecast page

| ID | Check | Steps | Expected | Result |
|---|---|---|---|---|
| F1 | Page loads | Open http://localhost:8080 | Logo and title, Refresh with "Latest measurement Sun 1 Nov, 13:30 IST (2 h ago)" under it, 14 city tiles with values and categories sorted most polluted first, the worst city's details shown | |
| F2 | City details | Click Delhi | Coloured box with value, category, AQI scale marker and time; outlook with Highest, Lowest, Change over 24 hours; trend sentence; health effect; stations and age; chart with a coloured box above each hour's point and a legend (Measured, Forecast, Peak, Low); tabs "What to do" and "Why this forecast" | |
| F3 | Switching cities | Click three other cities | Details change to each city; the selected tile has a dark outline and a tint of its colour | |
| F4 | Advice groups | In "What to do", switch "Advice for" through the 3 choices | Advice text and sources change; forecast numbers stay the same; the tab stays open | |
| F5 | Why this forecast | Open the "Why this forecast" tab | Six factors in plain words plus "All other factors combined"; bars left (lowers, blue) or right (raises, orange); no internal names like pm25_lag_6h | |
| F6 | Hover values | Hover over a box above the chart, then a point; select the chart and press the arrow keys | Box and point highlight together; a box with date and time, value, category, and "Measured" or "Forecast" with the change from the hour before; the first point is a diamond with an outlined box; highest forecast hour with an orange ring and "Peak", lowest with a blue ring and "Low"; labels never cut off at the edges | |
| F7 | Times and dates | Look at the chart's time labels and the boxes | Each box sits exactly above its point; times every 3 hours starting with the measured hour; the day under the first time and where a new day starts; a dashed midnight line through the boxes with the day name above; no "now" label | |
| F8 | AQI colours and labels | Compare tiles with the table in "How to read this page" | Each tile's colour matches its category; the category is also written as text | |
| F9 | Notes | Open a city with missing history (if any) | Yellow "Notes on this forecast" box in plain words; the missing reading is not listed in "Why this forecast" | |
| F10 | No forecast (stale data) | Do not refresh live features for more than 4 h, then open a city | "No forecast for … right now", reason with the age, last known reading, other cities in the region | |
| F11 | Model server down | Stop the model server, click a city | "Could not load the forecast … Model server unavailable"; page still usable | |
| F12 | Backend down | Stop the backend, press Refresh | Red bar explaining the service cannot be reached | |
| F13 | Loading message | Restart the backend, click a city immediately | "Loading the forecast for …" with a spinner until the details appear | |
| F14 | Refresh | Press Refresh after `dvc repro feast_online` | "Latest measurement …" updates; the open tab stays open | |
| F15 | Across India | Click a city's bar; open "By category", click a bar; press "Show all" | Most polluted and cleanest city (and cities without data, if any); "By city": bars sorted and coloured by category, a click opens that city; "By category": No data first and all six categories (0 shown), counts on top, hover lists the cities; a click switches to "By city" with those cities highlighted; "Show all" clears it; the word "null" never appears | |
| F16 | Help section | Open "How to read this page" | Explanation on the left (including measured ◆ vs forecast ●, Peak and Low), AQI table with colours on the right | |
| F17 | Dark theme | Press the moon button, reload the page | Dark colours everywhere including charts; after the reload it is still dark; the sun button switches back | |
| F18 | Keyboard use | Use Tab, Enter and the arrow keys only | Every tile, hour cell, tab and control can be reached; arrows move between tabs; a visible focus outline shows where you are | |

## Forecast page on a phone (400 px wide)

| ID | Check | Steps | Expected | Result |
|---|---|---|---|---|
| P1 | Phone layout | Open the page at phone width | Menu button and theme button at the top; City menu; no tiles; no horizontal scrolling of the page | |
| P2 | Menu | Tap the menu button | Forecast and Pipeline console links; tapping outside closes it | |
| P3 | City menu | Open the City menu | Cities from most to least polluted, with value and category | |
| P4 | Details | Choose a city | Details directly below, readable without zooming; outlook figures as compact rows; chart labels do not overlap | |
| P5 | Across India | Scroll down, tap a city's bar | That city opens and the page scrolls to its details | |

## Pipeline console

| ID | Check | Steps | Expected | Result |
|---|---|---|---|---|
| C1 | Page loads | Open http://localhost:8080/pipeline.html | Five summary cards with a status word each; tabs Workflows, Pipeline, Live data, Model, Monitoring | |
| C2 | Cards open tabs | Click each card | The matching tab opens; the address ends with #workflows, #pipeline, #live, #model or #monitoring; reloading keeps that tab | |
| C3 | Scheduled workflows | After an Airflow run, press Refresh; compare with the Airflow UI | One row per workflow: latest run, next run, how long it took, Succeeded/Failed as in Airflow; "Open in Airflow" opens that workflow; with Airflow stopped for over 45 min, *airsense_live* shows Overdue and the card says "Airflow may be stopped" | |
| C4 | Step status matches DVC | Run `dvc status`, compare with the Pipeline tab | Same steps shown as changed / up to date (the console may be up to 1 min behind) | |
| C5 | Step details | Click a step in the graph and a name in the table | Details beside the graph: command, last run, report values, and the reason if it changed; the table row is highlighted | |
| C6 | Speed chart | Compare with the step reports in `data/interim/` | One bar per step that reports timing; values match; numbers written 346,101 | |
| C7 | Collection timeline | Compare pink boxes with the snapshot files in `data/raw/live/datagov/` | Missing half-hours are pink; hover shows the time; gaps listed with times; if Airflow was stopped, an ongoing gap "still missing" (never "No gaps" with 0 collected) | |
| C8 | Model and gate | Compare with `data/interim/registry.json` and `gate_report.json` | Same version and metrics; 5 checks in plain words with ticks | |
| C9 | Warnings | Compare with the end of `logs/collector.log` | Latest warnings and errors listed, newest first; no API key visible | |
| C10 | Backend down | Stop the backend, press Refresh; then reload the page | Red bar explaining the status cannot be loaded; after a reload only the bar and the header, no empty sections | |
| C11 | Dark theme | Switch theme on the console | Cards, graph, tables and chart readable in both themes | |
| C12 | Phone width | Open at 400 px | Cards stacked; tables shown as stacked rows (label and value); graph hidden, the steps table is the list; long log lines wrap; the tab bar scrolls sideways and the selected tab is in view | |
| C13 | Monitoring tab | Open the Monitoring tab; compare with `data/monitoring/reports/*.json` | Forecast error, "no change" error, skill and pairs as in `evaluation.json`; a daily chart; the table by hours ahead ("1–6 hours ahead" …); drift bars with the 0.2 and 0.3 marks, values and levels as in `drift.json`; the latest retrain decision with its reason | |
| C14 | An alert on the console | Stop the model server for 3 min (`docker compose stop model-server`), refresh, then start it | Monitoring card red ("Problem"), "The model server is down" listed as Critical with what to check; after the restart it moves to *Recently resolved* with its duration within about 5 min | |
| C15 | Grafana panels | Open the Monitoring tab on the laptop; switch the theme | Four live panels load within a few seconds and follow the theme; the four links open Grafana, Prometheus and Alertmanager. On another device: a note that they work on the laptop only | |
| C16 | Error above the limit | When the model error is above its limit but better than "no change" | The Model error figure is amber with "above the limit, but better than “no change”: no alert" | |

## Result summary

| Date | Tester | Checks run | Passed | Failed (IDs) |
|---|---|---|---|---|
| | | | | |
