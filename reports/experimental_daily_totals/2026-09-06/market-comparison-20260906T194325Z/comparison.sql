-- Actual SQLite comparison query for the Sept. 6, 2026 provisional totals.
-- :model_json is the selected-candidate totals report; :quotes_json is the
-- captured public-page observation. The latter is descriptive only and is not
-- admitted bookmaker transport.
WITH
game_labels(game_pk, matchup) AS (
    VALUES
        (823253, 'New York Yankees at San Diego Padres'),
        (823091, 'Athletics at Seattle Mariners'),
        (824552, 'Minnesota Twins at Chicago White Sox'),
        (823903, 'Washington Nationals at Los Angeles Dodgers')
),
quote_game(event_id, game_pk) AS (
    VALUES
        ('34624714', 823253),
        ('34624715', 823091),
        ('34624716', 824552),
        ('34624717', 823903)
),
model AS (
    SELECT
        CAST(json_extract(game.value, '$.game_pk') AS INTEGER) AS game_pk,
        json_extract(game.value, '$.scheduled_start_utc') AS scheduled_start_utc,
        CAST(json_extract(game.value, '$.expected_total') AS REAL) AS expected_total,
        CAST(json_extract(game.value, '$.away_expected_runs') AS REAL) AS away_expected_runs,
        CAST(json_extract(game.value, '$.home_expected_runs') AS REAL) AS home_expected_runs,
        json_extract(game.value, '$.residual_samples') AS residual_samples_json
    FROM json_each(:model_json, '$.games') AS game
    WHERE json_extract(game.value, '$.status') = 'PROJECTED'
),
quotes AS (
    SELECT
        quote_game.game_pk,
        json_extract(quote.value, '$.displayed_matchup') AS displayed_matchup,
        json_extract(quote.value, '$.total.displayed_line') AS displayed_line_text,
        CAST(replace(json_extract(quote.value, '$.total.displayed_line'), '−', '-') AS REAL) AS displayed_line,
        CAST(replace(json_extract(quote.value, '$.total.over'), '−', '-') AS REAL) AS over_price_american,
        CAST(replace(json_extract(quote.value, '$.total.under'), '−', '-') AS REAL) AS under_price_american,
        json_extract(:quotes_json, '$.response_completed_at_utc') AS captured_at_utc,
        json_extract(:quotes_json, '$.provider_quote_updated_at_utc') AS provider_quote_updated_at_utc,
        json_extract(:quotes_json, '$.provider_quote_freshness') AS provider_quote_freshness,
        json_extract(:quotes_json, '$.observation_status') AS observation_status
    FROM json_each(:quotes_json, '$.rows') AS quote
    JOIN quote_game
      ON quote_game.event_id = json_extract(quote.value, '$.event_id')
),
residuals AS (
    SELECT
        model.game_pk,
        quotes.displayed_line,
        COUNT(*) AS residual_count,
        SUM(CASE WHEN CAST(sample.value AS REAL) > quotes.displayed_line THEN 1 ELSE 0 END) AS residual_over_count,
        SUM(CASE WHEN CAST(sample.value AS REAL) < quotes.displayed_line THEN 1 ELSE 0 END) AS residual_under_count,
        SUM(CASE WHEN CAST(sample.value AS REAL) = quotes.displayed_line THEN 1 ELSE 0 END) AS residual_push_count
    FROM model
    JOIN quotes ON quotes.game_pk = model.game_pk
    JOIN json_each(model.residual_samples_json) AS sample
    GROUP BY model.game_pk, quotes.displayed_line
),
joined AS (
    SELECT
        model.game_pk,
        game_labels.matchup,
        model.scheduled_start_utc,
        quotes.captured_at_utc,
        quotes.provider_quote_updated_at_utc,
        quotes.provider_quote_freshness,
        quotes.observation_status,
        quotes.displayed_matchup,
        quotes.displayed_line_text,
        quotes.displayed_line,
        quotes.over_price_american,
        quotes.under_price_american,
        model.away_expected_runs,
        model.home_expected_runs,
        model.expected_total,
        model.expected_total - quotes.displayed_line AS mean_minus_line,
        residuals.residual_count,
        residuals.residual_over_count,
        residuals.residual_under_count,
        residuals.residual_push_count
    FROM model
    JOIN quotes ON quotes.game_pk = model.game_pk
    JOIN game_labels ON game_labels.game_pk = model.game_pk
    JOIN residuals ON residuals.game_pk = model.game_pk
),
scored AS (
    SELECT
        joined.*,
        CAST(residual_over_count AS REAL) / NULLIF(residual_count, 0) AS residual_over_share,
        CAST(residual_under_count AS REAL) / NULLIF(residual_count, 0) AS residual_under_share,
        CAST(residual_push_count AS REAL) / NULLIF(residual_count, 0) AS residual_push_share,
        CAST(residual_over_count AS REAL) / NULLIF(residual_over_count + residual_under_count, 0) AS conditional_over_nonpush_share,
        CAST(residual_under_count AS REAL) / NULLIF(residual_over_count + residual_under_count, 0) AS conditional_under_nonpush_share,
        CASE
            WHEN over_price_american < 0 THEN -over_price_american / (-over_price_american + 100.0)
            ELSE 100.0 / (over_price_american + 100.0)
        END AS over_breakeven_probability,
        CASE
            WHEN under_price_american < 0 THEN -under_price_american / (-under_price_american + 100.0)
            ELSE 100.0 / (under_price_american + 100.0)
        END AS under_breakeven_probability
    FROM joined
)
SELECT
    game_pk,
    matchup,
    scheduled_start_utc,
    captured_at_utc,
    provider_quote_updated_at_utc,
    provider_quote_freshness,
    observation_status,
    displayed_matchup,
    displayed_line_text,
    displayed_line,
    over_price_american,
    under_price_american,
    away_expected_runs,
    home_expected_runs,
    expected_total,
    mean_minus_line,
    residual_count,
    residual_over_count,
    residual_under_count,
    residual_push_count,
    residual_over_share,
    residual_under_share,
    residual_push_share,
    conditional_over_nonpush_share,
    conditional_under_nonpush_share,
    over_breakeven_probability,
    under_breakeven_probability
FROM scored
ORDER BY scheduled_start_utc, game_pk;
