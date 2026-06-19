# Defects & Findings Lessons Learned

Internal Flask application for monitoring recurring defects and findings, reviewing repeated cases across projects, and creating Lessons Learned candidates in IBM Jazz.

## What it does

- Authenticates users against IBM Jazz and keeps an application-side session
- Loads defect and finding data from IBM Jazz / Report Builder
- Groups repeated cases based on normalized summaries and titles
- Supports Owner and Creator analysis views
- Exports analysis results and Lessons Learned selections to PDF
- Creates Lessons Learned candidate work items in IBM Jazz

## Stack

- Python
- Flask
- SQLite
- IBM Jazz / OSLC integration
- OpenAI API for optional semantic analysis
- ReportLab for PDF generation

## Repository note

This public export is anonymized. Internal people data, runtime databases, and organization-specific hosts were replaced with placeholders.
