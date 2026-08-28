from esg_watchdog.services.polling.krx import poll_krx_companies


def run() -> None:
    total_inserted = poll_krx_companies(trigger="cron")
    print(f"KRX polling completed: {total_inserted} inserted")


if __name__ == "__main__":
    run()