# Report Checker

Web-based PDF validation tool for functional test reports. The application runs document checks in the background and streams validation output to the browser.

## What it does

- Uploads PDF reports through a simple Flask interface
- Runs automated validation against report-format rules
- Detects missing references, missing comments, and inconsistent status patterns
- Highlights possible defects and structural issues inside the report
- Streams analysis progress to the UI with server-sent events

## Stack

- Python
- Flask
- pdfplumber
- PyPDF2
- pdf2image
- OpenCV

## Repository note

Runtime uploads and local Poppler bundles were intentionally excluded from this public export.
