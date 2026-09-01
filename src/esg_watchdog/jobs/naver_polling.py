from esg_watchdog.services.polling.naver import poll_naver_companies


def run() -> None:
    total_inserted = poll_naver_companies(trigger="cron")
    print(f"NAVER polling completed: {total_inserted} inserted")


if __name__ == "__main__":
    run()