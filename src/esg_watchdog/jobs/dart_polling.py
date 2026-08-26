from esg_watchdog.services.polling.dart import poll_dart_companies


def run() -> None:
    total_inserted = poll_dart_companies(trigger="cron")
    print(f"DART polling completed: {total_inserted} inserted")


if __name__ == "__main__":
    run()