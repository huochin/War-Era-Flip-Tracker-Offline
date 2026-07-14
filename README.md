# WarEra Flip Tracker — Offline / Desktop Version

A local desktop tool (Tkinter) that pulls WarEra trading history for a
Personal, Country, Party, or MU entity and computes FIFO flip-trading
profit — same math as the web version, but with a native GUI, a
persistent on-disk cache, and optional PNG dashboard exports.

## Requirements

- **Python 3.9+**
- **tkinter** — bundled with Python on Windows/macOS. On Linux you may
  need to install it separately: `sudo apt install python3-tk` (or your
  distro's equivalent).
- **Optional, only for PNG dashboard exports:**
  `pip install html2image`, plus Chrome or Chromium installed and
  discoverable on your system — `html2image` drives it directly to take
  the screenshot. Everything else in the tool runs on the standard
  library only; no other third-party packages are required.

## Running it

```bash
python3 main.py
```

This opens the GUI. There's no separate CLI mode — all input happens
through the form.

## Using the app

1. **Entity Type** — Personal, Country, Party, or MU.
2. **Input By** — search by **Name** (default) or by a raw **ID** if you
   already have one.
3. **ID / Name** — the value to search/track.
4. **Start / End** — optional date range. Leave both blank to pull full
   available history.
5. **API Key** — your WarEra API key. Typed into the form and reused
   across runs (see [Settings persistence](#settings-persistence) below).
6. Optional checkboxes:
   - **Save full transaction CSV** — dumps every raw transaction to
     `warera_transactions_<ID>.csv`.
   - **Use full cached history for reports** — ignores the date range
     for the *report*, but still uses it to decide what needs fetching;
     computes profit/dashboard over everything cached for that entity so
     far, and only fetches whatever's missing since the last run.
   - **Generate HTML summary dashboard** — same charts/layout as the web
     version, saved as a standalone `.html` file you can open in any
     browser.
   - **Save a picture summary of the HTML dashboard** — renders that
     dashboard to a `.png` (requires `html2image` + Chrome/Chromium, see
     above).
7. Click **Run**, or just press **Enter** anywhere in the form — it
   triggers the same action as clicking Run (this is disabled while a
   run is already in progress, and won't fire while a message dialog is
   open, since dialogs capture Enter first).
8. If searching by name, a **"Confirm entity"** dialog pops up for each
   candidate match, showing its resolved name and ID:
   - **Yes** — accept this candidate and run the tracker on it.
   - **No** — show the next candidate.
   - **Cancel** — stop the search entirely (the run is cancelled with no
     error message).
   - If every candidate is rejected, or none can be resolved at all
     (likely a bad API key or the API being down), you'll get a message
     explaining which case it was instead of a silent failure.

The **Delete Cache** button clears the cached transaction history for
whichever entity is currently entered, so the next run re-fetches
everything from scratch instead of extending the cache.

## Output layout

```
warera_data/<Entity Name>/transactions_cache.json      accumulating per-entity cache
warera_data/<Entity Name>/warera_transactions_<ID>.csv  optional, raw transactions
warera_data/<Entity Name>/warera_trading_detail_<ID>.csv
warera_data/<Entity Name>/warera_flip_profit_<ID>.csv
warera_data/<Entity Name>/warera_dashboard_<ID>.html    optional, standalone dashboard
warera_data/<Entity Name>/warera_dashboard_<ID>.png     optional, dashboard screenshot
warera_data/_registry.json                              ID -> folder name, so repeat runs reuse the same folder
warera_settings.json                                    last-used form values, incl. API key
```

Folder names are derived from the resolved entity name (sanitized for
filesystem safety), not the raw ID, so they stay readable. The registry
file maps IDs to folder names so re-running the same entity — even
under a slightly different search term — lands in the same folder
instead of creating duplicates.

## Caching behaviour

Every run appends newly-fetched transactions into
`transactions_cache.json` for that entity rather than replacing it, so
repeat runs only need to fetch what's new since last time. This is the
main practical difference from the web version, which has no persistent
storage and re-fetches the full range on every request.

- With **"Use full cached history"** unchecked (default): the date
  range you set controls *both* what gets fetched *and* what the
  report covers.
- With it checked: the date range still limits what gets fetched, but
  the report itself is computed over the entity's *entire* cache —
  useful for building up a complete picture over multiple runs without
  re-downloading everything each time.

## Dashboard PNG export

When "Save a picture summary" is checked, the tool renders the HTML
dashboard through a headless Chrome/Chromium instance (via
`html2image`) at a fixed width of 1220px, `--force-device-scale-factor=2`
for a crisp/retina-style image. The **height is computed dynamically**
per run — `base_height` covers the fixed charts/header/spacing, and 50px
is added per row in the "Still holding" table, so accounts with a lot of
open positions don't get their table cut off in the screenshot. If
`html2image` isn't installed, the tool logs a reminder to `pip install`
it and skips the PNG step without failing the rest of the run; if the
HTML dashboard wasn't separately requested, the temporary `.html` file
used to source the screenshot is deleted afterward, only the `.png`
sticks around.

## Settings persistence

`warera_settings.json` stores the last-used form values — including
your API key — so you don't have to retype them every run. This file is
created with owner-only permissions (`chmod 600`) on save, but it is
**not encrypted**. Treat it like any other local file containing a
secret: don't commit it, don't share it, and be mindful of who else has
access to the machine it's on.

## Differences from the web version

- **Persistent cache** (this version) vs. **fresh fetch every request**
  (web version, no server-side storage at all).
- **PNG export** (this version, needs Chrome/Chromium locally) vs.
  **no PNG export** (web version — a serverless function can't drive a
  real browser).
- **API key saved locally to disk** (this version) vs. **API key
  submitted per-request and never stored** (web version).
- Both share the exact same FIFO profit math and the same dashboard
  layout/charts — a dashboard generated here should look identical to
  one generated by the web version for the same data.
