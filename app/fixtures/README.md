# app/fixtures — 조회 계약 v1 기준 껍데기 (9/2 갱신, D-36 반영)

> ⚠️ **이 JSON 은 `DATABASE_URL` 이 없을 때의 fixture 모드 전용이다. 프로덕션 DB 에
> 적재하지 마라 (D-44).** 여기 담긴 씨앗 공약·사건은 9/6~9/7 교정에서 실제 보고서
> 문장·출처로 교체됐고, `load-fixtures` 로 다시 넣으면 그 교정이 되돌아간다.
> 아래 "9/3 완주 테스트 실패 → 계약 테이블에 직접 적재" 전제는 기록으로만 남긴다.

`app/lib/data.py`의 조회 함수 3개가 실DB 대신 읽는 JSON. **내용이 아니라 컬럼명이 이 파일들의 목적**이다.
기준은 DB 스키마 페이지 확정본(D-36)과 계약서 "조회 계약". 컬럼명·JSONB 키 철자는 마이그레이션과 같다.

- 내용은 전부 `(임시)` 표시. B의 씨앗 데이터(정답지-lite)가 나오면 값만 교체한다.
- **테이블 모양 그대로** 저장한다. "9/3 완주 테스트 실패 → 씨앗 데이터를 계약 테이블에 직접 적재" 보험이 이 형태를 전제로 한다.
- 조인은 `data.py`가 파이썬으로 한다. 실DB 전환 때 파일 로드를 SQL로 바꾸면 화면 코드는 그대로다.
- `scores`·`components` 값은 자리표시다. F-05 기본 산식과 수치가 일치하지 않아도 된다.

## 파일과 컬럼

| 파일 | 컬럼 |
|---|---|
| `companies.json` | id, stock_code, name, industry_key, is_control_group |
| `commitments.json` | id, company_id, category, sub_tags[], commitment_type, commitment_text, normalized_text, metric, target_value, target_year, baseline, source{doc_id,page,span}, filed_at, status, prompt_version |
| `events.json` | id, company_id, category, sub_tags[], event_type, title, summary, event_date, date_precision, reported_at, severity_signals{fine_amount,casualties,lawsuit,is_repeat,regulator}, is_subject, via_subsidiary, confirmed, is_retrospective, thin_source, evidence_quote, sources[], filing_ids[], source_count, prompt_version |
| `matches.json` | id, commitment_id, event_id, relation, rationale, evidence_quotes{commitment_quote,event_quote}, llm_confidence, gap_months, prompt_version, status, scores{materiality,confidence,components{industry_weight,relation_coef,severity,confirmed_coef}} |
| `alerts.json` | id, match_id, company_id, grade, headline, explanation, limitation, fallback, published_at, status |
| `articles.json` | id, url, press, title, published_at |
| `documents.json` | id, company_id, title, fiscal_year, published_at |

9/2 변경(D-36): events에 `category`·`sub_tags`·`reported_at`·`filing_ids` 추가, commitments에 `commitment_type` 추가, matches에서 `similarity` 삭제·`gap_months` 추가·`scores.components` 4키 고정·`status` 값은 pending/accepted/rejected, `llm_confidence`는 0~100 정수.

## 반환 타입 (고정)

```
get_alerts(filters)      → DataFrame
get_company(stock_code)  → {"company": dict, "commitments": DF, "events": DF, "alerts": DF, "positive_matches": DF}
get_alert_detail(id)     → {"alert": dict, "match": dict, "commitment": dict, "event": dict,
                            "articles": DF, "document": dict, "company": dict}
```

- E/S/G 필터는 `events.category`로 건다 (commitments 경유 아님, D-36).
- 이행긍정 배지는 `matches.relation == '이행긍정'` (D-12). 이 매칭은 경보를 만들지 않는다.
- 소급 사건(`is_retrospective`)은 `gap_months`를 `reported_at` 기준으로 계산한다 (D-10).

## 데이터 구성 (화면 엣지 케이스 검증용)

| 검증 대상 | 어디에 |
|---|---|
| 빈 화면 ("관측된 리스크 없음 · 대조군") | 오뚜기(007310) — 사건 0 · 매칭 0 · 경보 0, 공약 2건만, `is_control_group: true` |
| 이행긍정 배지 (D-12) | `matches` 3004 — 경보를 만들지 않는 유일한 매칭 |
| null 필드 | `alerts` 4003 `fallback: null`, `commitments` 1001 `target_value: null` |
| `date_precision` 분기 | `events` 2001 · 2007 = `month`, 나머지 = `day` |
| 소급 사건 | `events` 2001 `is_retrospective: true` (event_date 2024-03 · reported_at 2026-07-30) |
| 원문 링크 복수 | `events` 2002 · 2003 · 2006 = `sources` 2개 |
| 등급 3종 | 심각 1 · 경고 2 · 주의 3 |
| target_year 미도래 → 이행지연 | `matches` 3006 (2029년 목표) |
| 더보기 (D-19) | 경보 6건. 페이지 크기 5로 두면 1회 발생 |
| 최장 본문 레이아웃 | `alerts` 4001 explanation만 실제 길이(4단락 약 500자). 나머지는 자리표시 |

## 주의

- `articles.title`은 기사 제목이 아니라 **내용 요약 표현**이다. B가 씨앗 데이터 작성 시 실제 제목으로 교체한다.
- `scores.components`의 키 4개는 D-36으로 확정됐다. 화면은 키를 하드코딩하지 말고 dict를 순회해 막대를 그린다(순서만 industry_weight → severity → relation_coef → confirmed_coef).
