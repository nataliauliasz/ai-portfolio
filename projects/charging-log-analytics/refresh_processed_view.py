import logging

from app import create_connection, refresh_processed_source_view


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)


def main() -> None:
    LOGGER.info("Refreshing public.charging_log_processed_mv")
    with create_connection() as conn:
        refresh_processed_source_view(conn)
        conn.commit()
    LOGGER.info("Processed materialized view refreshed.")


if __name__ == "__main__":
    main()
