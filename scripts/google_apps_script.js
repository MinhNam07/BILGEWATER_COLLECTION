/**
 * Bilgewater Market → Google Sheets daily refresh
 *
 * Setup:
 * 1. Push this repo to GitHub (public repo, or use a token — see README).
 * 2. Replace CSV_URL below with your raw GitHub URL.
 * 3. Extensions → Apps Script → paste this file → Save.
 * 4. Run refreshBilgewater once to authorize.
 * 5. Triggers → Add trigger → refreshBilgewater → Time-driven → Day timer → 1am–2am.
 */

// TODO: replace with your repo URL (must point at bilgewater_latest.csv on main)
const CSV_URL =
  "https://raw.githubusercontent.com/YOUR_USER/bilgewater_collector/main/data/bilgewater_latest.csv";

const DATA_SHEET = "Data";
const LOG_SHEET = "Log";

function refreshBilgewater() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const dataSheet = getOrCreateSheet(ss, DATA_SHEET);
  const logSheet = getOrCreateSheet(ss, LOG_SHEET);

  if (logSheet.getLastRow() === 0) {
    logSheet.appendRow(["refreshed_at", "row_count", "status"]);
  }

  try {
    const response = UrlFetchApp.fetch(CSV_URL, { muteHttpExceptions: true });
    const code = response.getResponseCode();
    if (code !== 200) {
      throw new Error("HTTP " + code + ": " + response.getContentText().slice(0, 200));
    }

    const rows = Utilities.parseCsv(response.getContentText());
    if (!rows.length) {
      throw new Error("CSV is empty");
    }

    dataSheet.clear();
    dataSheet
      .getRange(1, 1, rows.length, rows[0].length)
      .setValues(rows);
    dataSheet.setFrozenRows(1);
    dataSheet.autoResizeColumns(1, rows[0].length);

    logSheet.appendRow([new Date(), rows.length - 1, "ok"]);
  } catch (err) {
    logSheet.appendRow([new Date(), 0, String(err)]);
    throw err;
  }
}

function getOrCreateSheet(ss, name) {
  return ss.getSheetByName(name) || ss.insertSheet(name);
}
