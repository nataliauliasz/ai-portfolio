# ReqIF PDF Anonymizer

Supporting project for anonymizing PDF and ReqIF-style document exports before sharing them outside an internal workflow.

## What it does

- Detects emails, phone numbers, person entities, and known client references
- Rewrites PDF text with anonymized placeholders
- Provides both a web interface and a desktop viewer workflow
- Supports configurable client dictionaries and page-level processing

## Stack

- Python
- FastAPI
- PyMuPDF
- spaCy
- HTML / JavaScript frontend

## Repository note

Client reference data was removed from this public export and replaced with a blank sample dictionary.
