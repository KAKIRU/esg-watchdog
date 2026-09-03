-- app_ro — 화면(app/) 전용 읽기 계정 (D-31 · D-35). 9/1 실행 완료, 기록용 플레이스홀더 버전.
-- <pw> · <owner> 는 플레이스홀더다. 실제 값은 .env(DATABASE_URL) 에만 두고 이 파일·로그·커밋에 쓰지 않는다.
--   <owner> = 테이블을 만드는 계정(alembic 을 돌리는 파이프라인 계정). 그 계정으로 접속해 확인: SELECT current_user;
-- 실행 예: sed 로 플레이스홀더를 바꾼 뒤 관리자 계정으로 psql -v ON_ERROR_STOP=1 -d esgwatchdog -f - 에 넘긴다.
-- 읽기 대상 7테이블 (D-35): companies · commitments · events · matches · alerts · articles · documents
--   (app/lib/data.py 의 TABLES 화이트리스트와 같다. 그 밖의 테이블도 SELECT 는 되지만 화면이 읽지 않는다)

CREATE ROLE app_ro LOGIN PASSWORD '<pw>';
GRANT CONNECT ON DATABASE esgwatchdog TO app_ro;
GRANT USAGE ON SCHEMA public TO app_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_ro;
-- 이후 마이그레이션으로 <owner> 가 새로 만드는 테이블도 자동으로 읽게 한다
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public GRANT SELECT ON TABLES TO app_ro;

-- 검증 절차 (프로브 테이블) — DEFAULT PRIVILEGES 가 실제로 먹는지, 쓰기가 막히는지 확인한다
--   1) <owner> 로 접속:  CREATE TABLE _probe_ro (id int);
--   2) app_ro 로 접속:   SELECT count(*) FROM _probe_ro;      → 0 이면 읽기 OK (기본 권한 적용됨)
--                        INSERT INTO _probe_ro VALUES (1);    → permission denied 여야 정상 (쓰기 불가)
--   3) <owner> 로 접속:  DROP TABLE _probe_ro;
--   4) app_ro 로 7테이블 각각 SELECT 1 FROM <table> LIMIT 1;  → 모두 성공
