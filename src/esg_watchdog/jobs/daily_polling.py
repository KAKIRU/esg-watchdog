from esg_watchdog.services.polling.dart import poll_dart_companies
from esg_watchdog.services.polling.krx import poll_krx_companies
from esg_watchdog.services.polling.naver import poll_naver_companies


def run() -> None:
    dart_inserted = poll_dart_companies(trigger="cron")
    print(f"DART polling completed: {dart_inserted} inserted")

    krx_inserted = poll_krx_companies(trigger="cron")
    print(f"KRX polling completed: {krx_inserted} inserted")

    naver_inserted = poll_naver_companies(trigger="cron")
    print(f"NAVER polling completed: {naver_inserted} inserted")


if __name__ == "__main__":
    run()