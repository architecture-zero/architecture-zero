-- The schema exactly as v0.1.0 created it.
--
-- Dumped from a running v0.1.0 (the tag, commit 18facd3) during the 2026-09-29
-- upgrade rehearsal, after its first boot and a first week of use:
--
--     sqlite3 backend/data/history.db .schema
--
-- Nothing below is edited by hand, and nothing should be. The point of this
-- file is that it is what a deployment of that release actually holds, which
-- is not always what reading models.py at the tag would lead you to expect.
-- tests/test_upgrade_from_release.py boots today's code on it.
CREATE TABLE users (
	id INTEGER NOT NULL, 
	username VARCHAR(255) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	role VARCHAR(50) NOT NULL, 
	permissions TEXT NOT NULL, 
	department VARCHAR(100) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at VARCHAR(50) NOT NULL, 
	mfa_secret VARCHAR(255), 
	mfa_enabled BOOLEAN NOT NULL, 
	failed_attempts INTEGER NOT NULL, 
	locked_until VARCHAR(50), 
	PRIMARY KEY (id), 
	UNIQUE (username)
);
CREATE TABLE messages (
	id INTEGER NOT NULL, 
	session VARCHAR(255) NOT NULL, 
	user_id INTEGER, 
	role VARCHAR(50) NOT NULL, 
	content TEXT NOT NULL, 
	model VARCHAR(100), 
	timestamp VARCHAR(50) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX idx_session ON messages (session);
CREATE INDEX idx_messages_user ON messages (user_id);
CREATE TABLE chat_sessions (
	id INTEGER NOT NULL, 
	session_id VARCHAR(255) NOT NULL, 
	user_id INTEGER, 
	name VARCHAR(300), 
	category VARCHAR(100) NOT NULL, 
	created_at VARCHAR(50) NOT NULL, 
	updated_at VARCHAR(50) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_sessions_sid_user UNIQUE (session_id, user_id)
);
CREATE INDEX idx_chat_sessions_user ON chat_sessions (user_id);
CREATE UNIQUE INDEX uq_chat_sessions_sid_guest ON chat_sessions (session_id) WHERE user_id IS NULL;
CREATE INDEX idx_chat_sessions_sid ON chat_sessions (session_id);
CREATE TABLE feedback (
	id INTEGER NOT NULL, 
	session_id VARCHAR(255) NOT NULL, 
	turn_index INTEGER NOT NULL, 
	value INTEGER NOT NULL, 
	created_at VARCHAR(50) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE config (
	"key" VARCHAR(255) NOT NULL, 
	value TEXT NOT NULL, 
	PRIMARY KEY ("key")
);
CREATE TABLE audit_log (
	id INTEGER NOT NULL, 
	user_id INTEGER, 
	username VARCHAR(100), 
	session_id VARCHAR(255) NOT NULL, 
	timestamp VARCHAR(50) NOT NULL, 
	prompt_hash VARCHAR(64) NOT NULL, 
	prompt_preview VARCHAR(200) NOT NULL, 
	response_length INTEGER NOT NULL, 
	model VARCHAR(100), 
	use_rag BOOLEAN NOT NULL, 
	sources TEXT NOT NULL, 
	duration_ms INTEGER, 
	ttft_ms INTEGER, 
	answer_lane VARCHAR(20), 
	rerank_ms INTEGER, 
	rerank_pool INTEGER, 
	rerank_provider VARCHAR(20), 
	PRIMARY KEY (id)
);
CREATE INDEX idx_audit_user ON audit_log (user_id);
CREATE INDEX idx_audit_ts ON audit_log (timestamp);
CREATE TABLE ingest_jobs (
	id INTEGER NOT NULL, 
	job_id VARCHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	source VARCHAR(500) NOT NULL, 
	department VARCHAR(100) NOT NULL, 
	chunks_processed INTEGER NOT NULL, 
	chunks_total INTEGER, 
	error TEXT, 
	created_at VARCHAR(50) NOT NULL, 
	completed_at VARCHAR(50), 
	PRIMARY KEY (id), 
	UNIQUE (job_id)
);
CREATE INDEX idx_ingest_jobs_created ON ingest_jobs (created_at);
CREATE TABLE eval_questions (
	id INTEGER NOT NULL, 
	question TEXT NOT NULL, 
	category VARCHAR(100) NOT NULL, 
	notes TEXT, 
	expected_source VARCHAR(255), 
	as_level INTEGER, 
	holdout INTEGER, 
	setup_turns TEXT, 
	created_at VARCHAR(50) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE eval_results (
	id INTEGER NOT NULL, 
	run_id VARCHAR(64) NOT NULL, 
	question_id INTEGER, 
	question_text TEXT NOT NULL, 
	category VARCHAR(100) NOT NULL, 
	response TEXT NOT NULL, 
	score INTEGER, 
	judge_rationale TEXT, 
	retrieved_sources TEXT, 
	retrieval_hit INTEGER, 
	retrieval_rank INTEGER, 
	context_text TEXT, 
	faithfulness INTEGER, 
	faithfulness_rationale TEXT, 
	freshness INTEGER, 
	freshness_rationale TEXT, 
	holdout INTEGER, 
	answer_model VARCHAR(100), 
	corpus_fingerprint VARCHAR(100), 
	judge_instrument VARCHAR(100), 
	run_at VARCHAR(50) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX idx_eval_results_run ON eval_results (run_id);
CREATE TABLE eval_judge_verdicts (
	id INTEGER NOT NULL, 
	result_id INTEGER NOT NULL, 
	judge_model VARCHAR(100) NOT NULL, 
	rubric VARCHAR(30) NOT NULL, 
	score INTEGER, 
	rationale TEXT, 
	judged_at VARCHAR(50) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_judge_verdict UNIQUE (result_id, judge_model, rubric)
);
CREATE INDEX idx_judge_verdicts_result ON eval_judge_verdicts (result_id);
CREATE TABLE quarantined_docs (
	id INTEGER NOT NULL, 
	source VARCHAR(500) NOT NULL, 
	department VARCHAR(100) NOT NULL, 
	trust_tier VARCHAR(20) NOT NULL, 
	text TEXT NOT NULL, 
	findings TEXT, 
	status VARCHAR(20) NOT NULL, 
	created_at VARCHAR(50) NOT NULL, 
	reviewed_at VARCHAR(50), 
	release_error TEXT, 
	PRIMARY KEY (id)
);
CREATE INDEX idx_quarantine_status ON quarantined_docs (status);
CREATE TABLE refresh_tokens (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	token_hash VARCHAR(255) NOT NULL, 
	expires_at VARCHAR(50) NOT NULL, 
	revoked BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	UNIQUE (token_hash)
);
CREATE INDEX idx_rt_token ON refresh_tokens (token_hash);
