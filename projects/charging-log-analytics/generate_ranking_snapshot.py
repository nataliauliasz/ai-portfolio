import argparse
import logging
import time

from app import generate_and_store_nightly_ranking_snapshot, sanitize_db_exception_message


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and store the global charging reliability ranking snapshot."
    )
    parser.add_argument(
        "--refresh-source-view",
        action="store_true",
        help="Refresh public.charging_log_processed_mv before building the ranking snapshot.",
    )
    parser.add_argument(
        "--allow-dynamic-session-fallback",
        action="store_true",
        help=(
            "Allow the emergency dynamic session SQL fallback when "
            "public.charging_log_sessions_mv is missing or incomplete."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    started_at = time.monotonic()

    try:
        LOGGER.info(
            (
                "Starting ranking snapshot generation. refresh_source_view=%s "
                "allow_dynamic_session_fallback=%s"
            ),
            args.refresh_source_view,
            args.allow_dynamic_session_fallback,
        )
        ranking = generate_and_store_nightly_ranking_snapshot(
            refresh_source_view=args.refresh_source_view,
            allow_dynamic_session_fallback=args.allow_dynamic_session_fallback,
        )
        duration_seconds = time.monotonic() - started_at
        LOGGER.info(
            "Ranking snapshot stored successfully. snapshot_date=%s source_relation=%s source_rows=%s sessions=%s duration_seconds=%.2f",
            ranking.get("snapshot_date"),
            ranking.get("source_relation"),
            ranking.get("source_row_count"),
            ranking.get("session_count"),
            duration_seconds,
        )
        return 0
    except Exception as exc:
        duration_seconds = time.monotonic() - started_at
        LOGGER.error(
            "Ranking snapshot generation failed after %.2f seconds: %s",
            duration_seconds,
            sanitize_db_exception_message(exc) or exc.__class__.__name__,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
