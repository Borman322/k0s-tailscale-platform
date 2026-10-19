CREATE TABLE IF NOT EXISTS jobs (
  id            BIGSERIAL PRIMARY KEY,
  source        TEXT NOT NULL,              -- greenhouse | lever | ashby
  company       TEXT NOT NULL,
  external_id   TEXT NOT NULL,
  title         TEXT NOT NULL,
  url           TEXT NOT NULL,
  location      TEXT,
  remote        BOOLEAN DEFAULT FALSE,
  region_lock   TEXT,                       -- us | eu | none | unknown
  score         INT DEFAULT 0,
  matched       TEXT[],                     -- какие ключевые слова сработали
  blockers      TEXT[],                     -- что мешает: us-only, eu-auth ...
  posted_at     TIMESTAMPTZ,
  first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_open       BOOLEAN NOT NULL DEFAULT TRUE,
  UNIQUE (source, company, external_id)
);

CREATE TABLE IF NOT EXISTS pipeline (
  job_id     BIGINT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  status     TEXT NOT NULL DEFAULT 'new',   -- new|interested|applied|screening|interview|offer|rejected|skip
  note       TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
  id         BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source     TEXT,
  company    TEXT,
  fetched    INT DEFAULT 0,
  inserted   INT DEFAULT 0,
  error      TEXT
);

CREATE INDEX IF NOT EXISTS jobs_score_idx   ON jobs (score DESC, first_seen DESC);
CREATE INDEX IF NOT EXISTS jobs_open_idx    ON jobs (is_open, first_seen DESC);
CREATE INDEX IF NOT EXISTS runs_started_idx ON runs (started_at DESC);

-- удобное представление для Grafana: вакансия + её статус
CREATE OR REPLACE VIEW v_board AS
SELECT j.id, j.first_seen, j.company, j.title, j.url, j.location,
       j.remote, j.region_lock, j.score,
       array_to_string(j.matched,  ', ') AS matched,
       array_to_string(j.blockers, ', ') AS blockers,
       COALESCE(p.status,'new') AS status, p.note, j.is_open
FROM jobs j LEFT JOIN pipeline p ON p.job_id = j.id;
