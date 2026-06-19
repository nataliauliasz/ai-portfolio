# Standards Reference Checker

FastAPI application for extracting referenced standards from uploaded documents and verifying whether matching files are available in an SVN-hosted standards repository.

## What it does

- Parses PDF, DOCX, and DOC files
- Detects referenced standards with regex-based extraction
- Checks matches against an indexed SVN repository
- Supports remote listing and local mirror workflows
- Returns links to matching standards and highlights missing references

## Stack

- Python
- FastAPI
- PyMuPDF
- python-docx
- SQLite
- SVN / WebDAV integration

## Repository note

This export uses placeholder infrastructure URLs and excludes private index data.
