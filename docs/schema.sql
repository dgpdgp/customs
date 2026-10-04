-- Схема БД (SQLite). Сгенерировано: python scripts/dump_schema.py > docs/schema.sql
-- Источник истины — app/models.py; таблицы создаются автоматически при старте приложения.

CREATE TABLE users (
	id INTEGER NOT NULL,
	email VARCHAR(255) NOT NULL,
	password_hash VARCHAR(255) NOT NULL,
	full_name VARCHAR(255),
	is_active BOOLEAN NOT NULL,
	is_admin BOOLEAN DEFAULT '0' NOT NULL,
	created_at DATETIME NOT NULL,
	last_login_at DATETIME,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_users_email ON users (email);

CREATE TABLE admin_audit_log (
	id INTEGER NOT NULL,
	user_id INTEGER,
	action VARCHAR(64) NOT NULL,
	detail TEXT,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_admin_audit_log_created_at ON admin_audit_log (created_at);
CREATE INDEX ix_admin_audit_log_user_id ON admin_audit_log (user_id);

CREATE TABLE app_settings (
	"key" VARCHAR(64) NOT NULL,
	value TEXT NOT NULL,
	updated_at DATETIME NOT NULL,
	updated_by_id INTEGER,
	PRIMARY KEY ("key"),
	FOREIGN KEY(updated_by_id) REFERENCES users (id) ON DELETE SET NULL
);


CREATE TABLE declaration_jobs (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	status VARCHAR(20) NOT NULL,
	error_message TEXT,
	files JSON NOT NULL,
	options JSON NOT NULL,
	reference_data JSON,
	proposed_data JSON,
	approved_data JSON,
	issues JSON NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	approved_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_declaration_jobs_status ON declaration_jobs (status);
CREATE INDEX ix_declaration_jobs_user_id ON declaration_jobs (user_id);

CREATE TABLE user_sessions (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	token_id VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	revoked_at DATETIME,
	ip_address VARCHAR(64),
	user_agent VARCHAR(512),
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX ix_user_sessions_token_id ON user_sessions (token_id);
CREATE INDEX ix_user_sessions_user_id ON user_sessions (user_id);

CREATE TABLE generation_logs (
	id INTEGER NOT NULL,
	job_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	event VARCHAR(32) NOT NULL,
	status VARCHAR(16) NOT NULL,
	model VARCHAR(64),
	input_tokens INTEGER,
	output_tokens INTEGER,
	duration_ms INTEGER,
	message TEXT,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(job_id) REFERENCES declaration_jobs (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_generation_logs_job_id ON generation_logs (job_id);
CREATE INDEX ix_generation_logs_user_id ON generation_logs (user_id);

