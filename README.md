# Price check

Source code for the **Price check** Streamlit in Snowflake app. This folder is the working copy. Snowflake holds the live version, and this repo is where edits are made and tracked.

## Where things live

| Item | Location |
|---|---|
| Local repo (this folder) | `/Users/luyin/price-check` |
| Snowflake app | `VENDOR_PORTFOLIO.REPORTING.PRICE_CHECK` (title: "Price check") |
| Snowflake connection | `jands` (account `gz71997.australia-east.azure`, warehouse `JANDS_WH`) |
| App owner | `SYSADMIN` |
| Git author | `lu.yy.33@gmail.com` |
| Git remote | https://github.com/ipman0924/Jprice.git (`origin`, branch `main`) |

## Files

- `streamlit_app.py`: the app (about 770 lines). Collects SKUs and target GP %, runs the what-if maths, draws charts and exports.
- `environment.yml`: Python packages for the Snowflake runtime.
- `pyproject.toml`: project metadata.
- `.streamlit/config.toml`: Streamlit settings.

## What the app depends on

The app does no history or cost logic itself. It reads from these SQL functions in `VENDOR_PORTFOLIO.REPORTING`:

- `F_PRICE_CHECK`
- `F_PRICE_CHECK_MONTHLY`
- `F_PRICE_CHECK_DEALSIZE`
- `F_PRICE_CHECK_HISTORY`

Changes to the numbers (cost, trend, deal size) may need changes to these functions, which live in Snowflake, not in this repo. Note any such change in the log below.

## Working on it

1. Edit files in this folder.
2. Check the syntax with `python3 -m py_compile streamlit_app.py`.
3. Commit with a message that says what changed and why.
4. Add a line to the change log below in the same commit.
5. To deploy to Snowflake, run the deploy from this folder. The deploy command is not yet confirmed for this app. Record the command and result in the log the first time it is used.

To get the live version again, use `GET 'snow://streamlit/VENDOR_PORTFOLIO.REPORTING.PRICE_CHECK/versions/live/'` through the `jands` connection.

## Change log

Newest first. Each entry gives the date, what changed, why, and whether it is deployed to Snowflake.

### 2026-10-27: Pushed to GitHub
- **What:** Added remote `origin` at `https://github.com/ipman0924/Jprice.git` and pushed `main`.
- **Why:** Keep the app source in GitHub alongside the change log.
- **Deployed:** n/a (no app change).

### 2026-10-27: Baseline
- **What:** Repo created from the live app (`VERSION$1`) as downloaded from Snowflake. Commit `27e575e`.
- **Why:** Start tracking the app in git so changes can be reviewed.
- **Deployed:** n/a (this is the existing live version).
- **Not yet done:** no remote, no changes, no SQL function changes.
