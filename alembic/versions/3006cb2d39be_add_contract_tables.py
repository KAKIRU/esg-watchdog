"""add contract tables, document_pages and column additions (D-36)

Revision ID: 3006cb2d39be
Revises: 6bab82d9fe05
Create Date: 2026-09-02 16:10:00.000000

손으로 작성한 리비전. 기존 테이블 drop/rename 없음, collection_cursors 는 유지 (D-28).
CHECK 값 목록과 server_default 는 src/esg_watchdog/models/*.py 와 같다.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3006cb2d39be'
down_revision: str | Sequence[str] | None = '6bab82d9fe05'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- 기존 테이블 컬럼 보강 -------------------------------------------------
    op.add_column('companies',
        sa.Column('exclude_terms', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False))
    op.add_column('documents', sa.Column('published_at', sa.Date(), nullable=True))
    op.add_column('documents', sa.Column('source_url', sa.Text(), nullable=True))
    op.add_column('filings', sa.Column('pblntf_ty', sa.String(length=1), nullable=True))
    op.add_column('filings', sa.Column('url', sa.Text(), nullable=True))
    op.add_column('article_companies', sa.Column('matched_alias', sa.Text(), nullable=True))
    op.add_column('pipeline_runs', sa.Column('stage', sa.Text(), nullable=True))
    op.add_column('pipeline_runs', sa.Column('note', sa.Text(), nullable=True))

    # --- document_pages -----------------------------------------------------
    op.create_table('document_pages',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('document_id', sa.BigInteger(), nullable=False),
    sa.Column('page_no', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'page_no', name='uq_document_pages_document_page')
    )
    op.create_index('idx_document_pages_document_page', 'document_pages', ['document_id', 'page_no'], unique=False)

    # --- commitments --------------------------------------------------------
    op.create_table('commitments',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('company_id', sa.BigInteger(), nullable=False),
    sa.Column('category', sa.String(length=1), nullable=False),
    sa.Column('sub_tags', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
    sa.Column('commitment_type', sa.Text(), nullable=False),
    sa.Column('commitment_text', sa.Text(), nullable=False),
    sa.Column('normalized_text', sa.Text(), nullable=True),
    sa.Column('metric', sa.Text(), nullable=True),
    sa.Column('target_value', sa.Numeric(), nullable=True),
    sa.Column('target_year', sa.Integer(), nullable=True),
    sa.Column('baseline', sa.Text(), nullable=True),
    sa.Column('source', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('filed_at', sa.Date(), nullable=False),
    sa.Column('status', sa.Text(), server_default=sa.text("'active'"), nullable=False),
    sa.Column('quarantine_reason', sa.Text(), nullable=True),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("category IN ('E', 'S', 'G')", name='ck_commitments_category'),
    sa.CheckConstraint("commitment_type IN ('정량', '정성')", name='ck_commitments_commitment_type'),
    sa.CheckConstraint("status IN ('active', 'quarantined')", name='ck_commitments_status'),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_commitments_company_category', 'commitments', ['company_id', 'category'], unique=False)

    # --- events -------------------------------------------------------------
    op.create_table('events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('company_id', sa.BigInteger(), nullable=False),
    sa.Column('category', sa.String(length=1), nullable=False),
    sa.Column('sub_tags', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('event_date', sa.Date(), nullable=False),
    sa.Column('date_precision', sa.Text(), server_default=sa.text("'day'"), nullable=False),
    sa.Column('reported_at', sa.Date(), nullable=True),
    sa.Column('severity_signals', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('is_subject', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('via_subsidiary', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('confirmed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('confirmed_basis', sa.Text(), nullable=True),
    sa.Column('is_retrospective', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('thin_source', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('evidence_quote', sa.Text(), nullable=False),
    sa.Column('sources', postgresql.ARRAY(sa.BigInteger()), server_default=sa.text("'{}'::bigint[]"), nullable=False),
    sa.Column('filing_ids', postgresql.ARRAY(sa.BigInteger()), server_default=sa.text("'{}'::bigint[]"), nullable=False),
    sa.Column('source_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("category IN ('E', 'S', 'G')", name='ck_events_category'),
    sa.CheckConstraint("date_precision IN ('day', 'month')", name='ck_events_date_precision'),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_events_company_category_date', 'events', ['company_id', 'category', 'event_date'], unique=False)

    # --- matches ------------------------------------------------------------
    op.create_table('matches',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('commitment_id', sa.BigInteger(), nullable=False),
    sa.Column('event_id', sa.BigInteger(), nullable=False),
    sa.Column('relation', sa.Text(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('evidence_quotes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('llm_confidence', sa.Integer(), nullable=False),
    sa.Column('gap_months', sa.Integer(), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('scores', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("relation IN ('위반', '후퇴', '이행지연', '이행긍정', '무관')", name='ck_matches_relation'),
    sa.CheckConstraint("status IN ('pending', 'accepted', 'rejected')", name='ck_matches_status'),
    sa.CheckConstraint('llm_confidence BETWEEN 0 AND 100', name='ck_matches_llm_confidence'),
    sa.ForeignKeyConstraint(['commitment_id'], ['commitments.id'], ),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('commitment_id', 'event_id', name='uq_matches_commitment_event')
    )

    # --- alerts -------------------------------------------------------------
    op.create_table('alerts',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('match_id', sa.BigInteger(), nullable=False),
    sa.Column('company_id', sa.BigInteger(), nullable=False),
    sa.Column('grade', sa.Text(), nullable=False),
    sa.Column('headline', sa.Text(), nullable=False),
    sa.Column('explanation', sa.Text(), nullable=False),
    sa.Column('limitation', sa.Text(), nullable=False),
    sa.Column('fallback', sa.Text(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Text(), server_default=sa.text("'published'"), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("grade IN ('주의', '경고', '심각')", name='ck_alerts_grade'),
    sa.CheckConstraint("status IN ('published', 'withdrawn')", name='ck_alerts_status'),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.ForeignKeyConstraint(['match_id'], ['matches.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('match_id')
    )
    op.create_index('idx_alerts_company_published', 'alerts', ['company_id', sa.text('published_at DESC')], unique=False)


def downgrade() -> None:
    """Downgrade schema. 새 테이블 drop + 추가 컬럼 drop 만."""
    op.drop_index('idx_alerts_company_published', table_name='alerts')
    op.drop_table('alerts')
    op.drop_table('matches')
    op.drop_index('idx_events_company_category_date', table_name='events')
    op.drop_table('events')
    op.drop_index('idx_commitments_company_category', table_name='commitments')
    op.drop_table('commitments')
    op.drop_index('idx_document_pages_document_page', table_name='document_pages')
    op.drop_table('document_pages')

    op.drop_column('pipeline_runs', 'note')
    op.drop_column('pipeline_runs', 'stage')
    op.drop_column('article_companies', 'matched_alias')
    op.drop_column('filings', 'url')
    op.drop_column('filings', 'pblntf_ty')
    op.drop_column('documents', 'source_url')
    op.drop_column('documents', 'published_at')
    op.drop_column('companies', 'exclude_terms')
