-- Schema and seed data for ClickHouse sync integration tests.
-- The database is created per test module; these statements run
-- against that temporary database.

-- Users table ------------------------------------------------------
CREATE TABLE users (
    id UInt32,
    name String,
    email Nullable(String),
    active UInt8
) ENGINE = MergeTree()
ORDER BY (id);

INSERT INTO users (id, name, email, active) VALUES
    (1, 'Alice', 'alice@example.com', 1),
    (2, 'Bob',   NULL,                 0),
    (3, 'Charlie', 'charlie@example.com', 1);

-- Orders table -----------------------------------------------------
CREATE TABLE orders (
    id UInt32,
    user_id UInt32,
    amount Float64
) ENGINE = MergeTree()
ORDER BY (id);

INSERT INTO orders (id, user_id, amount) VALUES
    (1, 1, 99.99),
    (2, 1, 24.50);

-- Additional tables used only for sync-state counts ----------------
CREATE TABLE orders_summing (
    id UInt32,
    user_id UInt32,
    amount Float64
) ENGINE = SummingMergeTree(amount)
ORDER BY (user_id, id);

CREATE TABLE events_replacing (
    id UInt32,
    user_id UInt32,
    event_type String
) ENGINE = ReplacingMergeTree()
ORDER BY (id);

-- AggregatingMergeTree table ---------------------------------------
CREATE TABLE agg_orders (
    user_id UInt32,
    amount_state AggregateFunction(sum, Float64)
) ENGINE = AggregatingMergeTree()
ORDER BY (user_id);

INSERT INTO agg_orders (user_id, amount_state)
SELECT user_id, sumState(amount) AS amount_state
FROM orders
GROUP BY user_id;

-- Kafka engine table (no data needed; tests rely on metadata only) -
CREATE TABLE kafka_events (
    key String,
    value String,
    topic String,
    partition UInt64,
    offset UInt64
) ENGINE = Kafka
SETTINGS
    kafka_broker_list = 'kafka:9092',
    kafka_topic_list = 'nao_sync_events',
    kafka_group_name = 'nao_sync_tests',
    kafka_format = 'JSONEachRow';

-- Materialized view + target table ---------------------------------
CREATE TABLE orders_by_user_mv_target (
    user_id UInt32,
    total_amount Float64
) ENGINE = SummingMergeTree(total_amount)
ORDER BY (user_id);

CREATE MATERIALIZED VIEW orders_by_user_mv
TO orders_by_user_mv_target AS
SELECT
    user_id,
    sum(amount) AS total_amount
FROM orders
GROUP BY user_id;

-- Dictionary (created from users table) -----------------------------
CREATE DICTIONARY users_dict (
    id UInt32,
    name String,
    email Nullable(String),
    active UInt8
)
PRIMARY KEY id
SOURCE(CLICKHOUSE(TABLE 'users'))
LAYOUT(FLAT())
LIFETIME(MIN 0 MAX 0);


