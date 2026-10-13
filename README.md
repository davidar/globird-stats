# GloBird Energy for Home Assistant

A small Home Assistant integration for [GloBird Energy](https://www.globirdenergy.com.au/) customers with a smart
electricity meter. It logs in to the GloBird customer portal, reads the half-hourly meter data and daily costs the
portal exposes, and writes them as hourly **Energy dashboard** statistics. It also provides an account balance sensor.

It deliberately does one thing: the Energy dashboard. For a full set of portal sensors see
[bolagnaise/globird-ha](https://github.com/bolagnaise/globird-ha), which this integration does not depend on.

## What you get

| | |
|---|---|
| `globird_stats:grid_import_<service>` | hourly grid import, kWh (statistic, for the Energy dashboard) |
| `globird_stats:grid_cost_<service>` | hourly cost, AUD (statistic, for the Energy dashboard) |
| `sensor.globird_balance` | account balance as the portal shows it (negative = in credit) |
| `sensor.globird_latest_data_day` | the newest day with a complete set of meter intervals |

One pair of statistics is created per electricity service with a smart meter. Gas services and basic
(manually read) meters are skipped.

## Installation

Through HACS: add this repository as a custom repository (category *Integration*), install, restart Home Assistant.
By hand: copy `custom_components/globird_stats` into your `config/custom_components/` and restart.

Then *Settings → Devices & services → Add integration → GloBird Energy* and sign in with your portal email and
password. If the portal asks for a captcha, log in once in a browser and try again.

In the Energy dashboard, add *Grid consumption* and pick the `grid_import` statistic; tick *Use an entity tracking
the total costs* and pick the matching `grid_cost` statistic.

## How the numbers are built

- The portal exposes **21 days** of half-hourly reads (48 per day, in NEM market time, which is AEST all year) and
  daily cost split into usage and supply charges, requested for the same days. Each pair of half-hours becomes one
  hour.
- The day's usage charge is spread over its hours in proportion to the metered kWh. On a flat tariff that is exact;
  on a time-of-use tariff the day's total is still exact but the split between hours is an approximation. The daily
  supply charge is booked in the day's first hour. Per-day totals match the portal to the cent.
- A day the portal marks as missing a meter read, or that does not yet have all 48 intervals, is left out until a
  later poll fills it in. Data for a day usually appears the next morning.
- Every poll continues the running total from what is already stored, so **history accumulates for as long as Home
  Assistant keeps running**; only the most recent 21 days can ever be back-filled. A host that is off for more than
  three weeks leaves a gap.
- Cost is only written for days that have a full set of meter intervals; the portal prices days further back than
  it shows intervals for, but those are not requested.
- The portal is polled once an hour. Nothing finer helps: new data lands once a day.

## Privacy and security

Your portal email and password are stored in Home Assistant's config entry storage, as every cloud integration does.
The password is sent to the portal RSA-encrypted, the same way the portal's own web app sends it. Diagnostics
downloads redact your email, addresses, account and meter identifiers.

## Development

```
python -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest -q
```

`series.py` is pure Python with no Home Assistant imports and holds the interval and cost arithmetic; it is tested on
its own. The portal client is tested against recorded response shapes, never the live portal.

## Status

Written for and used on one household's account. Issues and pull requests welcome, especially from
customers on time-of-use tariffs or with solar export, which the maintainer cannot test.

## Licence

MIT.
