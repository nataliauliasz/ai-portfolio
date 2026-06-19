# LL Cases Review Assistant

Flask-based review tool for working with XLSX and CSV case data, pulling IBM Jazz work item descriptions, and running AI-assisted coverage analysis for post-project review workflows.

## What it does

- Opens and filters multiple XLSX and CSV files
- Reads IBM Jazz work item descriptions through OSLC
- Sends structured review context to OpenAI for coverage analysis
- Returns AI-generated review output for further discussion
- Updates IBM Jazz `Check Date` for selected records

## Stack

- Python
- Flask
- IBM Jazz / OSLC integration
- OpenAI Responses API
- HTML / JavaScript frontend

## Repository note

The public export removes the original API key and replaces internal host defaults with placeholders.
