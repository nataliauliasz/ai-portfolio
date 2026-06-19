# Charging Log Analytics

In-progress Flask analytics application for reviewing charging test logs, detecting deviations, and generating ranking snapshots across projects, software versions, and devices.

## What it does

- Reads charging-log data from PostgreSQL
- Groups rows into charging sessions
- Builds anomaly-focused analysis views for problematic sessions
- Generates nightly ranking snapshots outside the HTTP request path
- Supports SQL refresh helpers and scheduler scripts for background updates

## Stack

- Python
- Flask
- PostgreSQL
- psycopg
- SQL materialized views
- Background analysis workers

## Repository note

This export excludes internal examples, local logs, private database configs, and organization-specific infrastructure values.
