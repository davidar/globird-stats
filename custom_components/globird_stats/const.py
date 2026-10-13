"""Constants for the GloBird Energy integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "globird_stats"
NAME = "GloBird Energy"

BASE_URL = "https://myaccount.globirdenergy.com.au"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

# The portal keeps 21 days of half-hourly reads (it prices further back, but cost without intervals cannot be
# spread across hours, so the cost request covers the same window).
USAGE_DAYS = 21
COST_DAYS = USAGE_DAYS

# New data lands once a day (mid-morning AEST); hourly polling is plenty.
UPDATE_INTERVAL = timedelta(hours=1)

STAT_GRID_IMPORT = "grid_import"
STAT_GRID_COST = "grid_cost"
